"""실험 세션 시험 (M5.1~5.3): 메모리로 GPU 고르기, 코드로 메모리 추정, 프로젝트 읽기, 명령 확인,
지표 표·그래프·요약, GLaDOS를 다시 켜도 이어서 모으기.
가짜 SSH 서버(test_remote.FakeServer)와 진짜 tmux, 시험 시계, 가짜 모델(FakeLLM)을 쓴다."""

from __future__ import annotations

import io
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest

asyncssh = pytest.importorskip("asyncssh")

from conftest import FakeLLM, act, answer, base_layers, make_ctx           # noqa: E402
from test_remote import GPUS, ME, FakeServer                               # noqa: E402
from test_runs import ARGS, Clock, make_runner, run_dir, seed_free, wait_for   # noqa: E402
from glados import cli                                                     # noqa: E402
from glados.config import LoopLimits, ensure_home, load_config            # noqa: E402
from glados.core import events as ev                                       # noqa: E402
from glados.core.loop import run_task                                      # noqa: E402
from glados.hands import Tool, ToolError, ToolRegistry                     # noqa: E402
from glados.hands import memest, metrics                                   # noqa: E402
from glados.safety import fair                                             # noqa: E402
from glados.safety.policy import Decision, Policy, ToolCall                # noqa: E402

TRAIN_PY = '''import argparse
from transformers import AutoModelForSequenceClassification
p = argparse.ArgumentParser()
p.add_argument("--model", default="bert-base-uncased")
p.add_argument("--batch", type=int, default=16)
p.add_argument("--max_len", type=int, default=128)
p.add_argument("--config", default="configs/base.yaml")
args = p.parse_args()
model = AutoModelForSequenceClassification.from_pretrained(args.model)
optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
if __name__ == "__main__":
    main()
'''
FACTS = {"model": "bert-base-uncased", "params_b": 0.11, "hidden": 768, "layers": 12, "vocab": 30522,
         "training": "full", "precision": "fp32", "optimizer": "adam", "batch": 32, "seq_len": 128, "grad_ckpt": False}
SPEC = {"x": "epoch", "metrics": [
    {"name": "train_loss", "key": "train_loss", "when": "", "better": "lower"},
    {"name": "dev_acc", "key": "acc", "when": "dev", "better": "higher"},
    {"name": "dev_loss", "key": "loss", "when": "dev", "better": "lower"}]}
LOG = ("epoch 1 train_loss 0.90\\n[dev] epoch 1 acc 0.71 loss 0.80\\nepoch 2 train_loss 0.60\\n"
       "[dev] epoch 2 acc 0.78 loss 0.65\\nepoch 3 train_loss 0.40\\n[dev] epoch 3 acc 0.83 loss 0.62\\n")


@pytest.fixture
def srv(tmp_path):
    if sys.platform == "win32":
        pytest.skip("the fake server runs commands with bash and tmux")
    s = FakeServer(tmp_path)
    code = s.work / "code"
    proj = code / "bert-cls"
    (proj / "configs").mkdir(parents=True)
    (proj / "scripts").mkdir()
    (proj / "README.md").write_text("# BERT classifier\n\nFine-tunes BERT on SST-2.\n\n## Run\n`python train.py --batch 32`\n")
    (proj / "train.py").write_text(TRAIN_PY)
    (proj / "configs" / "base.yaml").write_text("model: bert-base-uncased\nbatch: 16\nlr: 2.0e-5\n")
    (proj / "scripts" / "train.sh").write_text("#!/bin/bash\n# train\npython train.py --batch 32 --max_len 128\n")
    (proj / "requirements.txt").write_text("torch\ntransformers\n")
    git = ["git", "-c", "user.name=GLaDOS Test", "-c", "user.email=test@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=proj, check=True)
    subprocess.run(git + ["add", "."], cwd=proj, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "first"], cwd=proj, check=True)
    (code / "other").mkdir()
    (code / "other" / "run.sh").write_text("echo hi\n")
    (s.work / "proj").mkdir()                                                          # git이 아닌 폴더
    yield s
    s.close()


def runner_for(srv, tmp_path, llm=None, **kw):
    r, tools, notices, clock = make_runner(srv, tmp_path, **kw)
    r.llm = llm
    r.exp_root = tmp_path / "experiments"
    return r, tools, notices, clock


# ---------------------------------------------------------------------- M5.1 규칙 (Windows에서도)
def test_memory_fit_rule():
    s = lambda ts, mem, total=49140: fair.GpuSample(ts, mem, 0, 1, 0, total)   # noqa: E731  (다른 사람이 써도 됨)
    assert fair.need_with_margin(8192) == 8192 + 2048 and fair.need_with_margin(40960) == 40960 + 4096
    steady = [s(1000 + 60 * i, 9140) for i in range(7)]                                  # 40GB 남음, 6분
    assert fair.fits_for(steady, 1360, 20 * 1024) == (360.0, 40000.0)
    dip = steady[:2] + [s(1120, 30000)] + steady[3:]                                       # 2분째에 19GB로 줄었다
    held, low = fair.fits_for(dip, 1360, 20 * 1024)
    assert held == 180.0 and low == 40000.0                                                # 줄어든 뒤부터 다시 센다
    assert fair.fits_for(steady + [s(1420, 40000)], 1420, 20 * 1024) == (0.0, 9140.0)       # 지금 안 들어감
    assert fair.fits_for([fair.GpuSample(1000, 0, 0, 0, 0)], 1000, 1024) == (0.0, 0.0)      # 전체 메모리를 모름


def test_memory_estimate_formula():
    e = memest.estimate(FACTS)
    state = 0.11e9 * 16
    act = 32 * 128 * 768 * 12 * 34 * 4 / 2
    assert e.mb == pytest.approx((state + act) * 1.1 / 2**20 + 1536)
    assert e.basis == "bert-base-uncased · 0.11B · fp32 · full training · adam · batch 32 · seq 128" and not e.rough
    lora = memest.estimate({**FACTS, "model": "Llama-2-7b", "params_b": 7, "hidden": 4096, "layers": 32,
                            "training": "lora", "precision": "bf16", "batch": 4, "seq_len": 512})
    assert 25_000 < lora.mb < 30_000 and "LoRA" in lora.basis
    rough = memest.estimate({**FACTS, "batch": 0, "seq_len": 0, "hidden": 0, "layers": 0})
    assert rough.rough and rough.mb == pytest.approx(state * 1.3 * 1.1 / 2**20 + 1536)
    small = memest.estimate({**FACTS, "params_b": 0, "hidden": 512, "layers": 6, "vocab": 1000})
    assert "0.02B" in small.basis                                                          # hidden·layers로 크기 계산
    unknown = memest.estimate({**FACTS, "params_b": 0, "hidden": 0})
    assert unknown.mb == 0 and unknown.why == "the model size is not in the code"
    assert memest.check_facts({"training": "weird", "batch": -3, "precision": "fp8"})["training"] == "full"


def test_command_files():
    assert memest.command_files("python src/train.py --config configs/a.yaml --batch 32") == (
        ["src/train.py"], ["configs/a.yaml"])
    assert memest.command_files("torchrun --nproc_per_node 2 -m pkg.train --cfg=x.yml")[0] == [
        "pkg/train.py", "pkg/train/__main__.py"]
    assert memest.command_files("torchrun -m pkg.train --cfg=x.yml")[1] == ["x.yml"]
    assert memest.command_files("bash scripts/run.sh")[0] == ["scripts/run.sh"]


# ---------------------------------------------------------------------- M5.1 서버에서
def test_said_memory_can_share_a_gpu_with_a_warning(srv, tmp_path):
    r, tools, _, clock = runner_for(srv, tmp_path)
    seed_free(r, clock)
    args = {**ARGS, "command": "sleep 20", "gpus": [0], "mem_gb": 6}
    card = tools["remote.run"].preview(args)
    text = "\n".join(line for _, line in card)
    assert card[0] == ("fact", "EXP-01 · server-a · GPU 0 (남은 9.9GB) · 맞으면 바로 시작합니다")   # 다른 사람이 쓰는 GPU
    assert "메모리  GPU마다 6.0GB + 여유 2.0GB (연구원님이 정한 값)" in text
    assert ("decision", "! GPU 0는 다른 사람이 사용률 97%로 쓰고 있습니다. 같이 쓰면 많이 느려지고, 그쪽 메모리가 늘면 "
                        "메모리 부족이 날 수 있습니다") in card
    out = tools["remote.run"].run(args)
    assert out.startswith("EXP-01 started on server-a, GPU 0,") and "Memory: 6.0 GB per GPU (the Researcher said)." in out
    big = {**ARGS, "command": "sleep 20", "mem_gb": 8}                                    # 8GB + 2GB는 GPU 0에 안 들어감
    card = tools["remote.run"].preview(big)
    assert card[0][1].startswith("EXP-02 · server-a · GPU 1 (남은 47.7GB)")              # 혼자 쓰는 GPU, 많이 남은 것 먼저


def test_memory_need_waits_five_minutes_then_starts(srv, tmp_path):
    r, tools, notices, clock = runner_for(srv, tmp_path)
    r.can_wait = True
    args = {**ARGS, "command": "sleep 20", "mem_gb": 30}
    card = tools["remote.run"].preview(args)
    assert card[0][1] == "EXP-01 · server-a · GPU 1장 · 빈 GPU가 확인되면 자동으로 시작합니다"
    assert "GPU 0 남은 9.9GB" in card[1][1] and "GPU 1 남은 47.7GB, 확인 중 0초/5분" in card[1][1]
    out = tools["remote.run"].run(args)
    assert "needs 33.0 GB free (with margin) on 1 GPU(s) for 5 minutes" in out
    assert "GPU 1 has room, seen for 0 s" in out and "GPU 0 has 9.9 GB free" in out
    clock.at += 301
    r.tick()
    run = r.store.get("EXP-01")
    assert run.state == "running" and run.gpus == [1] and notices[-1].kind == "started"


def test_memory_estimated_from_the_code(srv, tmp_path):
    llm = FakeLLM([FACTS])
    r, tools, _, clock = runner_for(srv, tmp_path, llm=llm)
    seed_free(r, clock)
    card = tools["remote.run"].preview({"server": "server-a", "project": "bert-cls",
                                        "command": "python train.py --batch 32 --config configs/base.yaml"})
    text = "\n".join(line for _, line in card)
    assert "코드로 추정: bert-base-uncased · 0.11B · fp32 · full training · adam · batch 32 · seq 128" in text
    assert card[0][1].startswith("EXP-01 · server-a · GPU 1 (남은 47.7GB)")
    sent = llm.calls[0][0][1]["content"]
    assert sent.startswith("Command: python train.py --batch 32 --config configs/base.yaml")
    assert 'add_argument("--batch", type=int, default=16)' in sent and "from_pretrained(args.model)" in sent
    assert "### configs/base.yaml" in sent and "lr: 2.0e-5" in sent
    tools["remote.run"].preview({"server": "server-a", "project": "bert-cls",
                                 "command": "python train.py --batch 32 --config configs/base.yaml", "count": 1})
    assert len(llm.calls) == 1                                                            # 같은 명령은 다시 묻지 않음


def test_peak_memory_is_measured_and_used_next_time(srv, tmp_path):
    r, tools, _, clock = runner_for(srv, tmp_path)
    seed_free(r, clock)
    tools["remote.run"].run({**ARGS, "command": "sleep 3; echo done", "mem_gb": 20})
    gpus = json.loads(json.dumps(GPUS))
    gpus["gpus"][1]["mem"] = 5300
    gpus["apps"].append({"uuid": "GPU-b", "pid": 6161, "mem": 5000})
    gpus["owners"]["6161"] = [ME, "python"]
    srv.set_gpus(gpus)
    r.tick()
    assert r.store.get("EXP-01").peak_mb == 5000
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    r.tick()
    srv.set_gpus(GPUS)
    card = tools["remote.run"].preview({**ARGS, "command": "sleep 3; echo done"})
    assert ("plain", "메모리  GPU마다 4.9GB + 여유 2.0GB (EXP-01에서 잰 최대값)") in card
    card = tools["remote.run"].preview({**ARGS, "command": "sleep 3; echo again"})            # 다른 명령: 참고만
    assert card[2:4] == [("plain", "명령  sleep 3; echo again"), ("plain", f"폴더  {srv.work / 'proj'}")]
    assert ("decision", "메모리  필요량을 계산하지 못해 완전히 빈 GPU만 씁니다 (no model to read the code). "
                        "\"24GB 필요\"처럼 알려 주시면 그만큼 남은 GPU에서 돌립니다") in card


# ---------------------------------------------------------------------- M5.2 프로젝트
def test_projects_list_and_one_project(srv, tmp_path):
    _, tools, _, _ = runner_for(srv, tmp_path)
    out = tools["remote.projects"].run({"server": "server-a"})
    assert out.startswith(f"Projects in {srv.work / 'code'} on server-a (2):")
    line = next(x for x in out.splitlines() if x.startswith("- bert-cls: "))
    assert "git main" in line and "readme # BERT classifier Fine-tunes BERT on SST-2." in line and "train.py" in line
    assert "- other: files run.sh" in out
    out = tools["remote.projects"].run({"server": "server-a", "project": "bert-cls"})
    assert out.startswith(f"Project bert-cls on server-a ({srv.work / 'code' / 'bert-cls'}):")
    for part in ("### files (2 levels)", "configs/base.yaml", "### README.md", "Fine-tunes BERT on SST-2.",
                 "### train.py (arguments)", '5:p.add_argument("--batch", type=int, default=16)',
                 "### scripts/train.sh", "python train.py --batch 32 --max_len 128", "### configs/base.yaml",
                 "### requirements.txt: torch transformers", "To run it: remote.run with project=bert-cls"):
        assert part in out, part
    assert ".git/" not in out
    for args, key in (({"project": "nope"}, "tool.project_unknown"), ({"project": "../x"}, "tool.project_bad")):
        with pytest.raises(ToolError) as e:
            tools["remote.projects"].run({"server": "server-a", **args})
        assert e.value.key == key


def test_no_projects_folder(srv, tmp_path):
    import shutil
    shutil.rmtree(srv.work / "code")
    _, tools, _, _ = runner_for(srv, tmp_path)
    with pytest.raises(ToolError) as e:
        tools["remote.projects"].run({"server": "server-a"})
    assert e.value.key == "tool.project_none"


def test_run_in_a_project_and_confirm_wording(srv, tmp_path, monkeypatch):
    r, tools, _, clock = runner_for(srv, tmp_path)
    seed_free(r, clock)
    args = {"server": "server-a", "project": "bert-cls", "command": "python train.py --batch 32", "mem_gb": 4}
    card = tools["remote.run"].preview(args)
    assert ("plain", f"폴더  {srv.work / 'code' / 'bert-cls'}") in card
    assert any(line.startswith("커밋  ") and "(main) · 커밋 안 된 수정 없음" in line for _, line in card)
    tools["remote.run"].run(args)
    run = r.store.get("EXP-01")
    assert run.project == "bert-cls" and run.cwd.endswith("/code/bert-cls")
    with pytest.raises(ToolError) as e:
        tools["remote.run"].preview({**args, "project": "a/b"})
    assert e.value.key == "tool.project_bad"
    # 승인 상자: "이 명령어가 맞습니까?" + 맞습니다/아니요
    d = Decision("ask", "코드:서버실행", "", grantable=False, key="policy.remote_run")
    out = io.StringIO()
    home = tmp_path / "gh"
    ensure_home(home)
    a = cli.App(load_config(home), cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux")
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    try:
        assert a.approve(ToolCall("remote.run", args, card=card), d) == "deny"
    finally:
        a.close()
    shown = out.getvalue()
    assert "[승인 필요] 이 명령어가 맞습니까? 맞으면 서버에서 실행합니다" in shown
    assert "  1) 맞습니다, 실행   3) 아니요" in shown and "EXP-01 · server-a" in shown


def test_no_means_ask_what_to_change():
    tool = Tool("remote.run", "run", {"type": "object", "properties": {"server": {"type": "string"},
                                                                       "command": {"type": "string"}}},
                run=lambda a: "started", preview=lambda a: [("fact", "EXP-01")], deny_note="Ask what to change.")
    tools = ToolRegistry([tool])
    llm = FakeLLM([act("remote.run", server="server-a", command="python train.py"), answer("What should change?")])
    events = []
    run_task("run it", llm=llm, policy=Policy(base_layers()),
             ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools, limits=LoopLimits(),
             system_prompt="test", emit=events.append, approve=lambda call, d: "deny")
    assert [e.data["text"] for e in events if e.kind == ev.OBSERVATION] == ["Ask what to change."]


# ---------------------------------------------------------------------- M5.3 지표 (Windows에서도)
def test_pairs_and_extract():
    assert metrics.pairs("Epoch 3/10 - loss: 0.52 - val_acc: 0.81") == {"epoch": 3.0, "loss": 0.52, "val_acc": 0.81}
    assert metrics.pairs("{'loss': 0.52, 'learning_rate': 5e-05, 'epoch': 1.0}")["learning_rate"] == 5e-05
    assert metrics.pairs("12:00:01 INFO epoch 2 dev_acc 0.83") == {"epoch": 2.0, "dev_acc": 0.83}
    assert math.isnan(metrics.pairs("step 2 train_loss=nan")["train_loss"])
    text = LOG.replace("\\n", "\n") + "Epoch 4:  45%|####  | 450/1000 [00:10<00:12, loss=0.33]\r" \
                                      "Epoch 4: 100%|######| 1000/1000 [00:20<00:00, loss=0.31]\n"
    spec = metrics.check_spec(SPEC, {k for line in metrics.lines_of(text) for k in metrics.pairs(line)})
    cols, rows = metrics.extract(text, spec)
    assert cols == ["epoch", "train_loss", "dev_acc", "dev_loss"]
    assert rows[:3] == [{"epoch": 1, "train_loss": 0.9, "dev_acc": 0.71, "dev_loss": 0.8},
                        {"epoch": 2, "train_loss": 0.6, "dev_acc": 0.78, "dev_loss": 0.65},
                        {"epoch": 3, "train_loss": 0.4, "dev_acc": 0.83, "dev_loss": 0.62}]
    best = metrics.best(cols, rows, spec)
    assert best["dev_acc"] == {"value": 0.83, "x": 3, "last": 0.83, "better": "higher"}
    assert metrics.csv_text(cols, rows).splitlines()[1] == "1,0.9,0.71,0.8"
    assert metrics.md_table(cols, rows).splitlines()[2] == "| 1 | 0.9 | 0.71 | 0.8 |"
    with pytest.raises(ValueError):
        metrics.check_spec({"x": "epoch", "metrics": [{"name": "a", "key": "not_in_log", "when": "", "better": "lower"}]},
                           {"epoch", "loss"})
    dup = metrics.check_spec({"x": "nope", "metrics": [{"name": "loss", "key": "loss", "when": "", "better": "lower"},
                                                       {"name": "loss", "key": "acc", "when": "", "better": "higher"}]},
                             {"loss", "acc"})
    assert dup["x"] == "" and [m["name"] for m in dup["metrics"]] == ["loss", "loss_2"]
    cols, rows = metrics.extract("loss 0.5\nloss 0.4\n", {"x": "", "metrics": [dup["metrics"][0]]})
    assert cols == ["step", "loss"] and [r["step"] for r in rows] == [1, 2]


def test_plot_writes_a_png(tmp_path):
    spec = {"x": "epoch", "metrics": SPEC["metrics"]}
    cols, rows = metrics.extract(LOG.replace("\\n", "\n"), spec)
    assert metrics.plot(tmp_path / "m.png", "EXP-01 · test", cols, rows, spec)
    assert (tmp_path / "m.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_pattern_from_the_first_lines():
    lines = metrics.lines_of("loading model\n" + LOG.replace("\\n", "\n"))
    state: dict = {}
    found = [metrics.pairs(x) for x in lines]
    keep = [(f, x) for f, x in zip(found, lines) if f]
    new = metrics.note_keys(state, [f for f, _ in keep], [x for _, x in keep])
    assert new == ["epoch", "train_loss", "acc", "loss"] and state["counts"]["acc"] == 3
    body = metrics.sample_text(state, [x for _, x in keep])
    assert "- acc (3 lines), e.g. [dev] epoch 1 acc 0.71 loss 0.80" in body
    assert "First lines with numbers:\nepoch 1 train_loss 0.90" in body and "loading model" not in body
    llm = FakeLLM([SPEC])
    spec = metrics.ask_spec(llm, body, set(state["counts"]))
    assert [m["name"] for m in spec["metrics"]] == ["train_loss", "dev_acc", "dev_loss"]
    again = metrics.sample_text(state, [], ["f1"], spec)
    assert "The table so far: x=epoch; train_loss=train_loss, dev_acc=acc when 'dev'" in again
    assert "New keys appeared later: f1." in again
    rows: dict = {}
    st: dict = {}
    metrics.apply(lines[:3], spec, rows, st)                                          # 조각으로 나눠 넣어도
    metrics.apply(lines[3:], spec, rows, st)
    assert metrics.table(spec, rows) == metrics.extract("\n".join(lines), spec)
    md = metrics.md_table(*metrics.table(spec, {float(e): {"train_loss": 1.0 / e} for e in range(1, 501)}), limit=200)
    shown = md.splitlines()[2:]
    assert len(shown) == 200 and shown[0].startswith("| 1 |") and shown[-1].startswith("| 500 |")


def test_read_csv_round_trip(tmp_path):
    cols, rows = metrics.extract(LOG.replace("\\n", "\n"), SPEC)
    (tmp_path / "m.csv").write_text(metrics.csv_text(cols, rows))
    back = metrics.read_csv(tmp_path / "m.csv")
    assert metrics.table(SPEC, back) == (cols, rows)


# ---------------------------------------------------------------------- M5.3 서버에서: 표·그래프·요약, 이어서
def test_table_graph_and_summary_when_it_ends(srv, tmp_path):
    llm = FakeLLM([SPEC])
    r, tools, notices, clock = runner_for(srv, tmp_path, llm=llm)
    seed_free(r, clock)
    tools["remote.run"].run({"server": "server-a", "project": "bert-cls", "command": f"printf '{LOG}'", "mem_gb": 4})
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    r.tick()
    run = r.store.get("EXP-01")
    folder = Path(run.exp_dir)
    assert folder == tmp_path / "experiments" / "EXP-01_bert-cls"
    assert sorted(p.name for p in folder.iterdir()) == ["metrics.csv", "metrics.png", "summary.md"]   # 출력 글은 안 남김
    assert run.log_offset == (run_dir(srv, "EXP-01") / "output.log").stat().st_size
    assert (folder / "metrics.csv").read_text().splitlines() == [
        "epoch,train_loss,dev_acc,dev_loss", "1,0.9,0.71,0.8", "2,0.6,0.78,0.65", "3,0.4,0.83,0.62"]
    assert (folder / "metrics.png").read_bytes()[:4] == b"\x89PNG"
    md = (folder / "summary.md").read_text()
    assert md.startswith("# EXP-01 · bert-cls\n") and "## 지표 (전체 3줄, metrics.csv와 같음)" in md
    assert "| 1 | 0.9 | 0.71 | 0.8 |" in md and "| 3 | 0.4 | 0.83 | 0.62 |" in md
    assert "최고: train_loss 0.4 (epoch 3) · dev_acc 0.83 (epoch 3) · dev_loss 0.62 (epoch 3)" in md
    assert "- 메모리: GPU마다 4.0GB (연구원님이 정한 값)" in md and "![metrics](metrics.png)" in md
    assert "## 출력 끝\n\n```\nepoch 1 train_loss 0.90" in md
    for name in ("metrics.csv", "metrics.png", "summary.md"):                         # 서버의 실행 기록 폴더에도
        assert (run_dir(srv, "EXP-01") / name).read_bytes() == (folder / name).read_bytes(), name
    done = notices[-1]
    assert done.kind == "done" and done.lines()[-2:] == [
        "  최고: train_loss 0.4 (epoch 3) · dev_acc 0.83 (epoch 3) · dev_loss 0.62 (epoch 3)",
        f"  표·그래프·요약: {folder}"]
    out = tools["remote.log"].run({"run": "EXP-01"})
    assert "Project: bert-cls." in out and f"Experiment folder on this computer: {folder}." in out
    assert f"Metrics (x = epoch, 3 rows; table, graph and summary in {folder}):" in out
    assert "epoch 3: train_loss 0.4, dev_acc 0.83, dev_loss 0.62" in out
    assert "Best: train_loss 0.4 at epoch 3 (lower is better); dev_acc 0.83 at epoch 3 (higher is better)" in out
    assert len(llm.calls) == 1                                                        # 패턴은 한 번만 정한다


def epochs(a: int, b: int, dev: bool = True) -> str:
    """printf로 찍을 학습 출력 (epoch a..b, 숫자 줄 2개씩)."""
    out = []
    for e in range(a, b + 1):
        out.append(f"epoch {e} train_loss {1 / e:.3f}")
        if dev:
            out.append(f"[dev] epoch {e} acc {0.5 + e / 100:.2f} loss {1 - e / 100:.2f}")
    return "\\n".join(out) + "\\n"


def test_experiment_session_continues_after_glados_restarts(srv, tmp_path):
    llm = FakeLLM([SPEC])
    r, tools, _, clock = runner_for(srv, tmp_path, llm=llm)
    seed_free(r, clock)
    command = f"printf '{epochs(1, 20)}'; sleep 3; printf '{epochs(21, 22)}'"           # 앞 40줄, 3초 뒤 4줄
    tools["remote.run"].run({**ARGS, "command": command, "mem_gb": 4})
    server_log = run_dir(srv, "EXP-01") / "output.log"
    wait_for(server_log, "epoch 20 acc")
    r.collect(r.store.get("EXP-01"))                                                  # 실행 중 갱신 (5분마다)
    run = r.store.get("EXP-01")
    folder = Path(run.exp_dir)
    assert run.metric_spec and run.best["_rows"] == 20 and run.log_offset == server_log.stat().st_size
    assert not (folder / ".pending-lines.txt").exists() and len(llm.calls) == 1      # 앞 40줄로 패턴을 정함
    r.stop_watch()                                                                    # GLaDOS가 꺼짐 (실행은 계속)
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    r2, _, notices, _ = runner_for(srv, tmp_path, clock=Clock(time.time()), store=r.store)   # 다시 켬, 모델 없음
    r2.tick()
    run = r2.store.get("EXP-01")
    lines = (folder / "metrics.csv").read_text().splitlines()
    assert run.state == "done" and len(lines) == 23 and lines[1] == "1,1.0,0.51,0.99"
    assert lines[-1] == "22,0.045,0.72,0.78"                                          # 남은 출력만 이어서 읽음
    md = (folder / "summary.md").read_text()
    assert "## 지표 (전체 22줄, metrics.csv와 같음)" in md and "| 1 | 1 | 0.51 | 0.99 |" in md   # 모든 epoch
    assert notices[-1].kind == "done" and "GLaDOS가 꺼져 있는 동안 끝났습니다" in "\n".join(notices[-1].lines())


def test_new_kinds_of_lines_make_it_look_again(srv, tmp_path):
    train_only = {"x": "epoch", "metrics": [{"name": "train_loss", "key": "train_loss", "when": "", "better": "lower"}]}
    llm = FakeLLM([train_only, SPEC])
    r, tools, _, clock = runner_for(srv, tmp_path, llm=llm)
    seed_free(r, clock)
    command = f"printf '{epochs(1, 40, dev=False)}'; sleep 3; printf '{epochs(41, 42)}'"   # dev 줄은 나중에야
    tools["remote.run"].run({**ARGS, "command": command, "mem_gb": 4})
    server_log = run_dir(srv, "EXP-01") / "output.log"
    wait_for(server_log, "epoch 40 train")
    r.collect(r.store.get("EXP-01"))
    assert r.store.get("EXP-01").metric_spec["metrics"][0]["name"] == "train_loss"
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    r.tick()
    run = r.store.get("EXP-01")
    assert [m["name"] for m in run.metric_spec["metrics"]] == ["train_loss", "dev_acc", "dev_loss"]
    again = llm.calls[1][0][1]["content"]
    assert "New keys appeared later: acc, loss." in again and "The table so far: x=epoch; train_loss=train_loss." in again
    lines = (Path(run.exp_dir) / "metrics.csv").read_text().splitlines()
    assert lines[0] == "epoch,train_loss,dev_acc,dev_loss" and lines[40] == "40,0.025,," and lines[-1] == "42,0.024,0.92,0.58"


def test_chunks_cut_lines_and_korean_letters(srv, tmp_path, monkeypatch):
    from glados.hands import runner as runner_mod
    monkeypatch.setattr(runner_mod, "FETCH_BYTES", 37)                               # 줄과 한글 글자가 조각 사이에서 잘림
    r, tools, _, clock = runner_for(srv, tmp_path, llm=FakeLLM([SPEC]))
    seed_free(r, clock)
    log = LOG.replace("[dev]", "[검증 dev]")
    tools["remote.run"].run({**ARGS, "command": f"printf '{log}'", "mem_gb": 4})
    wait_for(run_dir(srv, "EXP-01") / "exit", "0 ")
    monkeypatch.setattr(runner_mod, "FINAL_ROUNDS", 100)
    r.tick()
    run = r.store.get("EXP-01")
    assert (Path(run.exp_dir) / "metrics.csv").read_text().splitlines()[1:] == ["1,0.9,0.71,0.8", "2,0.6,0.78,0.65",
                                                                               "3,0.4,0.83,0.62"]
    assert "[검증 dev] epoch 3 acc 0.83 loss 0.62" in run.metric_state["tail"]


def test_app_puts_experiments_in_the_workspace(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    (home / "servers.toml").write_text('[[server]]\nalias = "server-a"\n', encoding="utf-8")
    (home / "folders.toml").write_text(f'workspace = "{(tmp_path / "ws").as_posix()}"\n', encoding="utf-8")
    llm = FakeLLM([])
    a = cli.App(load_config(home), cli.Screen(ansi=False, out=io.StringIO()), llm=llm, os_name="linux")
    try:
        assert a.runner.exp_root == tmp_path / "ws" / "experiments" and a.runner.llm is llm
        assert a.cfg.servers[0].projects == "code" and "remote.projects" in a.tools.names()
    finally:
        a.close()
