"""서버 실행 시험 (M5): 가짜 SSH 서버(test_remote.FakeServer)에서 진짜 tmux로 실행을 띄운다.
GPU는 가짜 nvidia-smi가 답하고, 시계는 시험이 돌린다 (5분 규칙, 최대 실행 시간). 인터넷·진짜 서버는 쓰지 않는다."""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

asyncssh = pytest.importorskip("asyncssh")

from conftest import FakeLLM, act, answer, base_layers, check, make_ctx    # noqa: E402
from test_remote import FakeServer, ME                                     # noqa: E402
from glados import cli, notify                                             # noqa: E402
from glados.config import LoopLimits, Server, ensure_home, load_config     # noqa: E402
from glados.core import events as ev                                       # noqa: E402
from glados.core.loop import run_task                                      # noqa: E402
from glados.hands import HandContext, Tool, ToolError, ToolRegistry, tool_error   # noqa: E402
from glados.hands import remote, runner as runner_mod                      # noqa: E402
from glados.memory.runs import Run, RunStore                               # noqa: E402
from glados.safety import fair                                             # noqa: E402
from glados.safety.policy import Policy                                    # noqa: E402

WED_2PM = datetime(2026, 11, 11, 14, 0).timestamp()      # 평일 낮 (동시 1장, 최대 4시간)


@pytest.fixture
def srv(tmp_path):
    if sys.platform == "win32":
        pytest.skip("the fake server runs commands with bash and tmux")
    s = FakeServer(tmp_path)
    proj = s.work / "proj"
    proj.mkdir()
    (proj / "train.py").write_text("print('train')\n")
    git = ["git", "-c", "user.name=GLaDOS Test", "-c", "user.email=test@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=proj, check=True)
    subprocess.run(git + ["add", "."], cwd=proj, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "first"], cwd=proj, check=True)
    (proj / "train.py").write_text("print('train v2')\n")             # 커밋 안 된 수정
    yield s
    s.close()


class Clock:
    def __init__(self, at: float = WED_2PM):
        self.at = at

    def __call__(self) -> float:
        return self.at


def make_runner(srv: FakeServer, tmp_path: Path, clock: Clock | None = None, store: RunStore | None = None):
    clock = clock or Clock()
    hctx = HandContext(places=make_ctx("linux").places, home=tmp_path / "glados_home",
                       servers=[Server("server-a", gpus=4, workdir=str(srv.work), tmux_prefix="glados-test")],
                       hub=srv.hub())
    hctx.runner = runner_mod.Runner(hctx.hub, fair.GpuWatch(), hctx.servers,
                                    store or RunStore(hctx.home), log_path=hctx.home / "logs" / "runs.jsonl",
                                    now=clock)
    tools = {t.name: t for t in remote.remote_tools(hctx)}
    r = hctx.runner
    notices: list = []
    r.on_notice = notices.append
    return r, tools, notices, clock


def seed_free(r, clock: Clock, minutes: int = 6) -> None:
    """GPU를 몇 분 전에 본 것으로 (5분 규칙을 채운다)."""
    remote.sample_gpus(r.hub, r.watch, "server-a", clock() - minutes * 60)


def wait_for(path: Path, text: str = "", seconds: float = 15) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            body = path.read_text(errors="replace")
            if text in body:
                return body
        time.sleep(0.1)
    raise AssertionError(f"{path} never had {text!r}")


def run_dir(srv: FakeServer, rid: str) -> Path:
    return srv.work / ".glados" / "runs" / rid


ARGS = {"server": "server-a", "cwd": "proj"}


# ---------------------------------------------------------------------- 시작·승인 카드
def test_card_then_start_on_a_free_gpu(srv, tmp_path):
    r, tools, notices, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    args = {**ARGS, "command": 'echo hello; echo "cuda=$CUDA_VISIBLE_DEVICES order=$CUDA_DEVICE_ORDER"; exit 3'}
    card = tools["remote.run"].preview(args)
    text = "\n".join(line for _, line in card)
    assert card[0] == ("fact", "EXP-01 · server-a · GPU 1 (빔) · 승인하면 바로 시작합니다")
    assert "명령  echo hello" in text and f"폴더  {srv.work / 'proj'}" in text
    assert "(main) · 커밋 안 된 파일 1개는 diff.patch로 저장" in text
    assert "tmux  glados-test-exp01 · 보기: tmux attach -r -t glados-test-exp01" in text
    assert "동시 1장(평일 낮) · 사용 중 1장 · 오늘 0.0/12 GPU시간 · 최대 4시간 · nice 10" in text
    assert any(style == "decision" and "동시 사용 한도를 넘습니다" in line for style, line in card)   # GPU 2가 이미 내 것
    out = tools["remote.run"].run(args)
    assert out.startswith("EXP-01 started on server-a, GPU 1, in tmux session glados-test-exp01")
    assert "1 uncommitted file(s) saved in" in out and "remote.log run=EXP-01" in out
    folder = run_dir(srv, "EXP-01")
    assert "train v2" in (folder / "diff.patch").read_text()                        # 커밋 안 된 수정이 남는다
    assert (folder / "command.sh").read_text().strip().startswith("echo hello")
    assert "nice -n 10" in (folder / "run.sh").read_text()
    wait_for(folder / "exit", "3 ")
    log = (folder / "output.log").read_text()
    assert "hello" in log and "cuda=1 order=PCI_BUS_ID" in log                     # GPU 번호는 nvidia-smi 순서
    r.tick()
    run = r.store.get("EXP-01")
    assert run.state == "failed" and run.exit_code == 3 and run.gpus == [1] and run.commit
    assert [n.kind for n in notices] == ["failed"]
    assert notices[0].lines()[0].startswith("EXP-01 실패: server-a, 종료 코드 3")
    assert "마지막 줄: cuda=1 order=PCI_BUS_ID" in notices[0].lines()[1]
    assert srv.tmux("has-session", "-t", "=glados-test-exp01").returncode != 0    # 끝나면 세션도 닫힌다


def test_queue_waits_for_the_five_minute_rule_then_starts(srv, tmp_path):
    r, tools, notices, clock = make_runner(srv, tmp_path)
    r.can_wait = True
    args = {**ARGS, "command": "echo step 1 loss 0.9; echo finished"}
    card = tools["remote.run"].preview(args)
    assert card[0][1] == "EXP-01 · server-a · GPU 1장 · 빈 GPU가 확인되면 자동으로 시작합니다"
    assert "GPU 1 확인 중 0초/5분" in card[1][1] and "GPU 0 다른 사람 사용 중" in card[1][1]
    out = tools["remote.run"].run(args)
    assert out.startswith("EXP-01 is queued on server-a: needs 1 free GPU(s); now: ")
    assert r.store.get("EXP-01").state == "waiting" and not run_dir(srv, "EXP-01").exists()
    clock.at += 120
    r.tick()
    assert r.store.get("EXP-01").state == "waiting" and not notices               # 아직 2분
    clock.at += 200
    r.tick()
    run = r.store.get("EXP-01")
    assert run.state == "running" and run.gpus == [1]
    assert notices[-1].kind == "started" and notices[-1].lines()[0] == "EXP-01 시작: server-a GPU 1 (5분 기다림)"
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    r.tick()
    assert r.store.get("EXP-01").state == "done"
    assert notices[-1].kind == "done" and "마지막 줄: finished" in notices[-1].lines()[1]


def test_one_shot_cannot_wait(srv, tmp_path):
    r, tools, _, _ = make_runner(srv, tmp_path)
    args = {**ARGS, "command": "python train.py"}
    card = tools["remote.run"].preview(args)
    assert card[-1] == ("decision", "! 한 줄 실행은 기다릴 수 없습니다: 빈 GPU가 확인되지 않으면 시작하지 않습니다")
    out = tools["remote.run"].run(args)
    assert out.startswith("Not started: needs 1 free GPU(s)") and r.store.get("EXP-01").state == "cancelled"


def test_two_queued_runs_do_not_take_the_same_gpu(srv, tmp_path):
    r, tools, notices, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    r.can_wait = True
    tools["remote.run"].run({**ARGS, "command": "sleep 30"})
    out = tools["remote.run"].run({**ARGS, "command": "sleep 30"})
    assert out.startswith("EXP-02 is queued") and "GPU 1 given to another of your runs" in out
    assert [x.state for x in r.store.all()] == ["waiting", "running"]


def test_nan_notice_once_then_stop_with_ctrl_c(srv, tmp_path):
    r, tools, notices, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    tools["remote.run"].run({**ARGS, "command": "echo 'step 1 train_loss=0.52'; echo 'step 2 train_loss=nan'; sleep 60"})
    wait_for(run_dir(srv, "EXP-01") / "output.log", "nan")
    r.tick()
    r.tick()
    assert [n.kind for n in notices] == ["nan"] and notices[0].decision
    assert notices[0].lines()[0] == ('EXP-01: 손실이 NaN입니다 (step 2 train_loss=nan). 멈추려면 "EXP-01 멈춰"라고 하세요')
    out = tools["remote.stop"].run({"run": "exp-01"})
    assert out.startswith("EXP-01 on server-a is stopped (it stopped after Ctrl+C")
    run = r.store.get("EXP-01")
    assert run.state == "stopped" and run.exit_code == 130
    assert srv.tmux("has-session", "-t", "=glados-test-exp01").returncode != 0
    with pytest.raises(ToolError) as e:
        tools["remote.stop"].run({"run": "EXP-01"})
    assert e.value.key == "tool.run_ended"


def test_time_limit_then_lost(srv, tmp_path):
    r, tools, notices, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    tools["remote.run"].run({**ARGS, "command": "sleep 60"})
    clock.at += 4 * 3600 + 60                                                       # 평일 낮에 시작: 4시간
    r.tick()
    assert [n.kind for n in notices] == ["time"] and notices[0].decision
    assert "최대 실행 시간 4시간(평일 낮)에 이르렀습니다" in notices[0].lines()[0]
    srv.tmux("kill-session", "-t", "=glados-test-exp01")                         # GLaDOS 밖에서 닫힘
    pid = int((run_dir(srv, "EXP-01") / "pid").read_text())
    end = time.monotonic() + 10
    while time.monotonic() < end and subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0:
        time.sleep(0.1)
    r.tick()
    assert notices[-1].kind == "lost" and r.store.get("EXP-01").state == "lost"


def test_restart_catches_up_and_cancels_old_queue(srv, tmp_path):
    r, tools, _, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    tools["remote.run"].run({**ARGS, "command": "echo bye"})
    r.can_wait = True
    tools["remote.run"].run({**ARGS, "command": "sleep 5"})                       # GPU 1이 배정돼서 기다림
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    store = r.store
    r2, _, notices, _ = make_runner(srv, tmp_path, clock=Clock(time.time() + 5), store=store)   # 다시 켬
    r2.expire_waiting()
    r2.tick()
    kinds = {n.kind: n for n in notices if n.kind != "budget"}       # 오후에는 하루 GPU 시간 알림도 (가짜 시작 시각 탓)
    assert set(kinds) == {"expired", "done"}
    assert "GLaDOS가 꺼져 있는 동안 끝났습니다" in kinds["done"].lines()[-1]
    assert store.get("EXP-02").state == "cancelled" and store.get("EXP-01").state == "done"


def test_remote_log_run_and_summary(srv, tmp_path):
    r, tools, _, clock = make_runner(srv, tmp_path)
    seed_free(r, clock)
    tools["remote.run"].run({**ARGS, "command": "for i in 1 2 3; do echo step $i loss 0.$i; done"})
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    out = tools["remote.log"].run({"run": "EXP-01", "lines": 2})
    assert out.startswith("EXP-01 on server-a: finished at ") and "exit code 0" in out
    assert "Command: for i in 1 2 3" in out and "tmux session glados-test-exp01" in out
    assert "step 2 loss 0.2\nstep 3 loss 0.3\n>>>" in out and "step 1" not in out.split("<<<")[1]
    assert r.summary_line().startswith("Your runs: EXP-01 done ") and r.summary_line().endswith("on server-a, exit 0.")
    with pytest.raises(ToolError) as e:
        tools["remote.log"].run({"run": "EXP-09"})
    assert e.value.key == "tool.run_unknown"
    lines = r.runs_lines()
    assert lines[0].startswith("서버 실행 1개 (이 컴퓨터의 기록")
    assert lines[1].startswith("  EXP-01  끝남 · server-a GPU 1 · ") and lines[1].endswith(" · for i in 1 2 3; do echo step $i loss 0.$i; done")


def test_run_id_skips_folders_already_on_the_server(srv, tmp_path):
    (srv.work / ".glados" / "runs" / "EXP-07").mkdir(parents=True)
    r, tools, _, _ = make_runner(srv, tmp_path)
    assert tools["remote.run"].preview({**ARGS, "command": "true"})[0][1].startswith("EXP-08 · ")


def test_errors_before_asking(srv, tmp_path):
    r, tools, _, _ = make_runner(srv, tmp_path)
    for args, key in (({**ARGS, "command": "x", "cwd": "nope"}, "tool.run_no_cwd"),
                      ({**ARGS, "command": "x", "gpus": [7]}, "tool.run_bad_gpu"),
                      ({**ARGS, "command": "x", "count": 9}, "tool.run_too_many"),
                      ({**ARGS, "command": " "}, "tool.remote_empty")):
        with pytest.raises(ToolError) as e:
            tools["remote.run"].preview(args)
        assert e.value.key == key, args
    card = tools["remote.run"].preview({"server": "server-a", "command": "ls"})     # 작업 폴더는 git이 아님
    assert ("plain", "커밋  git 저장소가 아니라 코드 상태를 저장하지 못합니다") in card


# ---------------------------------------------------------------------- 규칙 (가짜 서버 없이, Windows에서도)
def test_nan_lines():
    assert runner_mod.nan_line("step 1 loss 0.3\nstep 2 loss nan\n") == "step 2 loss nan"
    for line in ("{'loss': nan, 'lr': 0.1}", "Loss: NaN", "val_loss=-inf", "train/loss inf", "ppl: nan"):
        assert runner_mod.nan_line(line), line
    for line in ("loss 0.3 info", "nancy loss 0.2", "lossless 0.1 banana", "grad_norm nan", "loss 0.1 infinite"):
        assert not runner_mod.nan_line(line), line


def test_run_time_and_budget_rules():
    assert fair.max_hours(datetime(2026, 11, 11, 14)) == 4 and fair.max_hours(datetime(2026, 11, 11, 20)) == 12
    assert fair.max_hours(datetime(2026, 11, 14, 14)) == 12                       # 토요일
    now = datetime(2026, 11, 11, 12).timestamp()
    day = fair.day_start(now)
    spans = [(day - 3600, day + 3600, 2), (now - 1800, 0.0, 1), (0.0, 0.0, 1)]
    assert fair.gpu_hours(spans, day, now) == pytest.approx(2.0 + 0.5)


def test_run_store_ids(tmp_path):
    store = RunStore(tmp_path)
    assert store.next_id() == "EXP-01" and store.next_id({"EXP-41", "junk"}) == "EXP-42"
    store.save(Run("EXP-03", "server-a", "python a.py", created=1.0, state="running"))
    store.save(Run("EXP-04", "server-a", "python b.py", created=2.0))
    assert store.next_id() == "EXP-05" and [x.id for x in store.active()] == ["EXP-03", "EXP-04"]
    assert store.get("exp-3").command == "python a.py" and store.get("EXP-99") is None


@pytest.mark.parametrize("tool,args,verdict,rule", [
    ("remote.run", {"command": "python train.py --lr 3e-4"}, "ask", "코드:서버실행"),
    ("remote.run", {"command": "sudo python train.py"}, "deny", "코드:공유서버"),
    ("remote.run", {"command": "python train.py; pkill -f other"}, "deny", "코드:공유서버"),
    ("remote.run", {"command": "python train.py", "cwd": "/home/kim/proj"}, "deny", "코드:남의파일"),
    ("remote.stop", {"run": "EXP-01"}, "ask", "코드:실행멈춤"),
    ("remote.exec", {"command": "nohup python train.py"}, "ask", "코드:서버실행"),
    ("remote.exec", {"command": "python train.py &"}, "ask", "코드:서버실행"),
    ("remote.exec", {"command": "tmux new -d -s x python a.py"}, "ask", "코드:서버실행"),
    ("remote.exec", {"command": "tmux kill-session -t glados-test-exp01"}, "deny", "코드:공유서버"),
    ("remote.exec", {"command": "tmux ls"}, "allow", "기본:remote.exec:tmux ls"),
])
def test_run_policy(policy, tool, args, verdict, rule):
    ctx = make_ctx("linux", remote_user=ME, remote_dirs={"server-a": f"/home/{ME}/workspace"})
    d = check(policy, "linux", tool, ctx=ctx, server="server-a", **args)
    assert (d.verdict, d.rule) == (verdict, rule)
    if verdict == "ask" and tool != "remote.exec" or rule == "코드:서버실행":
        assert not d.grantable                                                     # "이 작업 동안" 없음: 매번


def test_run_asks_even_if_policy_file_allows(tmp_path):
    layers = base_layers() + [("연구원님", {"remote.run": {"*": "allow"}, "remote.stop": {"*": "deny"}})]
    p = Policy(layers)
    ctx = make_ctx("linux", remote_user=ME)
    ctx.grants.add(("remote.run", "코드:서버실행"))
    assert check(p, "linux", "remote.run", ctx=ctx, server="server-a", command="python a.py").verdict == "ask"
    assert check(p, "linux", "remote.stop", ctx=ctx, run="EXP-01").verdict == "deny"   # 더 엄한 쪽은 그대로


# ---------------------------------------------------------------------- 루프: 승인 전에 카드
def test_loop_shows_the_card_before_asking_and_skips_asking_on_errors():
    seen = []

    def preview(args):
        if args["command"] == "bad":
            raise tool_error("tool.run_no_tmux", server="server-a")
        return [("fact", "EXP-01 · server-a · GPU 1")]

    tool = Tool("remote.run", "run", {"type": "object", "properties": {"server": {"type": "string"},
                                                                       "command": {"type": "string"}}},
                run=lambda a: "EXP-01 started", preview=preview)
    tools = ToolRegistry([tool])
    llm = FakeLLM([act("remote.run", server="server-a", command="bad"),
                   act("remote.run", server="server-a", command="python a.py"), answer("EXP-01 started.")])
    events = []
    result = run_task("run it", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", emit=events.append,
                      approve=lambda call, d: seen.append((call.args["command"], list(call.card))) or "once")
    assert result.status == "done"
    assert seen == [("python a.py", [("fact", "EXP-01 · server-a · GPU 1")])]     # 오류 난 것은 묻지 않음
    obs = [e.data["text"] for e in events if e.kind == ev.OBSERVATION]
    assert obs[0].startswith("Error: tmux is not installed on server-a") and obs[1] == "EXP-01 started"


# ---------------------------------------------------------------------- 화면: /runs, 알림, OS 알림
def test_app_runs_command_prompt_and_notice(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ensure_home(home)
    (home / "servers.toml").write_text('[[server]]\nalias = "server-a"\ngpus = 4\nworkdir = "/home/u/w"\n',
                                       encoding="utf-8")
    cfg = load_config(home)
    out = io.StringIO()
    a = cli.App(cfg, cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux")
    sent = []
    monkeypatch.setattr(notify, "os_notify", lambda title, body, os_name: sent.append((title, body, os_name)))
    try:
        assert a.command("/runs") is False and "서버 실행 기록이 없습니다" in out.getvalue()
        a.runner.store.save(Run("EXP-01", "server-a", "python train.py", state="running", gpus=[1],
                                session="glados-u-exp01", created=time.time(), started=time.time() - 3700))
        a.command("/runs")
        text = out.getvalue()
        assert "  EXP-01  실행 중 · server-a GPU 1 · 1시간 1분 · python train.py" in text
        assert "보기: tmux attach -r -t glados-u-exp01" in text
        prompt = a.system_prompt()
        assert "Never train via remote.exec" in prompt and "remote.run project=<name>" in prompt
        assert "Your runs: EXP-01 running on server-a GPU 1 (1h 1m)." in prompt
        a.notice(runner_mod.Notice("nan", "EXP-01", "notice.nan", {"run": "EXP-01", "line": "loss nan"},
                                   decision=True))
        assert "결정 필요  EXP-01: 손실이 NaN입니다 (loss nan)" in out.getvalue()
        assert sent == [("GLaDOS · EXP-01", 'EXP-01: 손실이 NaN입니다 (loss nan). 멈추려면 "EXP-01 멈춰"라고 하세요',
                         "linux")]
        assert a.history_budget() > 2500                                           # 16k가 아니어도 확인 (32k 기본)
    finally:
        a.close()


def test_old_queue_is_cancelled_when_the_app_opens(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    (home / "servers.toml").write_text('[[server]]\nalias = "server-a"\n', encoding="utf-8")
    RunStore(home).save(Run("EXP-02", "server-a", "python a.py", state="waiting", session_id="old", created=1.0))
    out = io.StringIO()
    a = cli.App(load_config(home), cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux")
    try:
        assert a.runner.pending and a.runner.store.get("EXP-02").state == "cancelled"
        a.runner.on_notice = a.notice
        a.cfg.notify = False
        a.runner.flush()
        assert "EXP-02: GLaDOS가 꺼질 때 빈 GPU를 기다리던 중이라 시작하지 않았습니다" in out.getvalue()
    finally:
        a.close()


def test_os_notification_commands():
    cmd = notify.command("GLaDOS · EXP-01", "끝남 <ok> & 'quoted'", "windows")
    assert cmd[0] == "powershell.exe" and cmd[-2] == "-EncodedCommand"
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert "<text>끝남 &lt;ok&gt; &amp; ''quoted''</text>" in script               # XML·PowerShell 따옴표 둘 다
    assert "ToastNotificationManager" in script and "CreateToastNotifier" in script
    mac = notify.command("t", "b", "macos")
    assert mac[0] == "osascript" and mac[-2:] == ["t", "b"]                         # 글은 인자로 (스크립트에 안 섞음)
