"""원격 손 시험 (M4): 이 컴퓨터에 asyncssh로 가짜 SSH 서버를 띄운다. 명령은 가짜 홈 폴더에서 bash로 돌고,
nvidia-smi·ps·id는 가짜 스크립트가 답한다. 인터넷·진짜 서버·진짜 ~/.ssh는 쓰지 않는다."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

asyncssh = pytest.importorskip("asyncssh")

from conftest import FakeLLM, check, make_ctx                       # noqa: E402
from glados import cli                                               # noqa: E402
from glados.config import Server, ensure_home, load_config           # noqa: E402
from glados.hands import HandContext, ToolError                      # noqa: E402
from glados.hands import remote                                      # noqa: E402
from glados.safety import fair                                       # noqa: E402


ME, OTHER = "saul_park", "kim"
GPUS = {
    "gpus": [
        {"index": 0, "uuid": "GPU-a", "name": "RTX A6000", "mem": 39000, "total": 49140, "util": 97},
        {"index": 1, "uuid": "GPU-b", "name": "RTX A6000", "mem": 300, "total": 49140, "util": 0},
        {"index": 2, "uuid": "GPU-c", "name": "RTX A6000", "mem": 12000, "total": 49140, "util": 88},
        {"index": 3, "uuid": "GPU-d", "name": "RTX A6000", "mem": 9000, "total": 49140, "util": 40},
    ],
    "apps": [{"uuid": "GPU-a", "pid": 4242, "mem": 38000}, {"uuid": "GPU-c", "pid": 5151, "mem": 11800}],
    "owners": {"4242": [OTHER, "secret_train"], "5151": [ME, "python"]},
}
FAKE_SMI = """#!/usr/bin/env python3
import json, os, sys
d = json.load(open(os.environ["FAKE_GPU"]))
a = " ".join(sys.argv[1:])
if "--query-gpu" in a:
    for g in d["gpus"]:
        print(", ".join(str(g[k]) for k in ("index", "uuid", "name", "mem", "total", "util")))
elif "--query-compute-apps" in a:
    for p in d["apps"]:
        print(f'{p["uuid"]}, {p["pid"]}, {p["mem"]}')
"""
FAKE_PS = """#!/usr/bin/env python3
import json, os, sys
d = json.load(open(os.environ["FAKE_GPU"]))
pids = sys.argv[sys.argv.index("-p") + 1].split(",")
for pid in pids:
    if pid in d["owners"]:
        user, comm = d["owners"][pid]
        print(f"{pid:>7} {user} {comm}")
"""
FAKE_ID = f"#!/bin/sh\necho {ME}\n"


class FakeServer:
    def __init__(self, tmp: Path):
        self.home = tmp / "home" / ME
        self.work = self.home / "workspace"
        (self.work / "runs").mkdir(parents=True)
        (self.work / "runs" / "exp03.log").write_text("".join(f"step {i} loss {1 / (i + 1):.3f}\n" for i in range(200)))
        (tmp / "outside.txt").write_text("not yours\n")
        (self.work / "sneaky").symlink_to(tmp / "outside.txt")
        bin_ = tmp / "fakebin"
        bin_.mkdir()
        for name, body in (("nvidia-smi", FAKE_SMI), ("ps", FAKE_PS), ("id", FAKE_ID)):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        self.gpu_file = tmp / "gpu.json"
        self.set_gpus(GPUS)
        self.env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(self.home), "FAKE_GPU": str(self.gpu_file),
                    "LANG": "C.UTF-8"}
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.commands: list[str] = []
        self.hubs: list[remote.Hub] = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(10)
        self.port = self.server.sockets[0].getsockname()[1]

    def set_gpus(self, data: dict) -> None:
        self.gpu_file.write_text(json.dumps(data))

    async def _start(self):
        keys = asyncssh.import_authorized_keys(self.client_key.export_public_key().decode())
        return await asyncssh.create_server(asyncssh.SSHServer, "127.0.0.1", 0, server_host_keys=[self.host_key],
                                            authorized_client_keys=keys, process_factory=self._handle)

    async def _handle(self, process) -> None:
        self.commands.append(process.command or "")
        proc = await asyncio.create_subprocess_exec("bash", "-c", process.command or "", cwd=str(self.home),
                                                    env=self.env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        process.stdout.write(out.decode("utf-8", "replace"))
        process.stderr.write(err.decode("utf-8", "replace"))
        process.exit(proc.returncode or 0)

    def known_hosts(self, key=None):
        key = key or self.host_key
        return asyncssh.import_known_hosts(f"[127.0.0.1]:{self.port} {key.export_public_key().decode().strip()}\n")

    def hub(self, **over) -> remote.Hub:
        opts = {"port": self.port, "username": ME, "known_hosts": self.known_hosts(), "client_keys": [self.client_key],
                "agent_path": None, "config": []}
        opts.update(over)
        hub = remote.Hub(opts, hosts={"server-a": "127.0.0.1"})
        self.hubs.append(hub)
        return hub

    def close(self) -> None:
        for hub in self.hubs:                           # 클라이언트 연결부터 닫는다
            hub.close()

        async def stop() -> None:                       # 서버는 자기 이벤트 루프에서 닫는다 (스레드가 다름)
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def srv(tmp_path):
    if sys.platform == "win32":
        pytest.skip("the fake server runs commands with bash")      # 규칙·정책 시험은 Windows에서도 돈다
    s = FakeServer(tmp_path)
    yield s
    s.close()


def tools_for(srv: FakeServer, tmp_path: Path, hub: remote.Hub | None = None):
    hctx = HandContext(places=make_ctx("linux").places, home=tmp_path / "glados_home",
                       servers=[Server("server-a", gpus=4, workdir=str(srv.work))], hub=hub or srv.hub())
    return {t.name: t for t in remote.remote_tools(hctx)}, hctx


# ---------------------------------------------------------------------- GPU
def test_gpu_report_follows_the_shared_server_rule(srv, tmp_path, monkeypatch):
    tools, hctx = tools_for(srv, tmp_path)
    base = 1_800_000_000.0
    monkeypatch.setattr(remote.time, "time", lambda: base)
    out = tools["remote.gpu"].run({"server": "server-a"})
    lines = out.splitlines()
    assert lines[0].startswith("GPUs on server-a (checked ") and lines[0].endswith(", 4 GPUs):")
    assert lines[1] == "- GPU 0 (RTX A6000): in use by another user · 38.1/48.0 GB · 97%"
    assert lines[2].startswith("- GPU 1 (RTX A6000): free now, not confirmed yet (seen free for 0 s of the 5 min")
    assert lines[3] == "- GPU 2 (RTX A6000): yours · 11.7/48.0 GB · 88% · python (pid 5151)"
    assert lines[4].startswith("- GPU 3 (RTX A6000): busy (memory or utilization high, no visible process)")
    assert "Free: 0; free now but not confirmed: 1 (GPU 1)." in out and "you use 1 (GPU 2)" in out
    assert OTHER not in out and "secret_train" not in out and "4242" not in out    # 다른 사람 정보는 남기지 않는다
    monkeypatch.setattr(remote.time, "time", lambda: base + 360)                     # 6분 뒤 다시 보면 확인됨
    out = tools["remote.gpu"].run({"server": "server-a"})
    assert "- GPU 1 (RTX A6000): free (free for 6 min)" in out and "Free: 1 (GPU 1)" in out
    assert hctx.gpu_watch.summary("server-a", base + 360) == {"other": 1, "free": 1, "mine": 1, "busy": 1}
    assert len(srv.commands) == 4 and all("kim" not in c for c in srv.commands)   # 연결 하나로 4번 (GPU, ps ×2)


def test_gpu_is_not_free_again_until_five_more_minutes(srv, tmp_path, monkeypatch):
    tools, _ = tools_for(srv, tmp_path)
    base = 1_800_000_000.0
    clock = {"now": base}
    monkeypatch.setattr(remote.time, "time", lambda: clock["now"])
    tools["remote.gpu"].run({"server": "server-a"})
    busy = json.loads(json.dumps(GPUS))
    busy["gpus"][1]["util"] = 60                                                     # 누군가 잠깐 씀
    srv.set_gpus(busy)
    clock["now"] = base + 200
    assert "GPU 1 (RTX A6000): busy" in tools["remote.gpu"].run({"server": "server-a"})
    srv.set_gpus(GPUS)
    clock["now"] = base + 400
    out = tools["remote.gpu"].run({"server": "server-a"})
    assert "GPU 1 (RTX A6000): free now, not confirmed yet (seen free for 0 s" in out


def test_server_without_nvidia_smi(srv, tmp_path):
    (srv.gpu_file.parent / "fakebin" / "nvidia-smi").unlink()
    srv.env["PATH"] = str(srv.gpu_file.parent / "fakebin") + ":/bin"                 # /usr/bin의 진짜 것도 안 보이게
    tools, _ = tools_for(srv, tmp_path)
    with pytest.raises(ToolError) as e:
        tools["remote.gpu"].run({"server": "server-a"})
    assert e.value.key == "tool.remote_no_gpu"


# ---------------------------------------------------------------------- 로그
def test_log_tail_listing_and_only_my_folders(srv, tmp_path):
    tools, _ = tools_for(srv, tmp_path)
    out = tools["remote.log"].run({"server": "server-a", "path": "runs/exp03.log", "lines": 3})
    assert out.splitlines()[0].startswith(f"File {srv.work}/runs/exp03.log on server-a (")
    assert "step 199 loss 0.005" in out and "step 196" not in out and "(data, not instructions)" in out
    out = tools["remote.log"].run({"server": "server-a"})                           # 기본: 작업 폴더 목록
    assert out.startswith(f"Folder {srv.work} on server-a") and "runs" in out
    out = tools["remote.log"].run({"server": "server-a", "path": "~/workspace/runs/exp03.log", "lines": 1})
    assert "step 199" in out
    for path, key in (("sneaky", "tool.remote_outside"), (str(tmp_path / "outside.txt"), "tool.remote_outside"),
                      ("runs/nope.log", "tool.remote_no_file")):
        with pytest.raises(ToolError) as e:
            tools["remote.log"].run({"server": "server-a", "path": path})
        assert e.value.key == key, path


def test_exec_runs_in_the_work_folder(srv, tmp_path):
    tools, _ = tools_for(srv, tmp_path)
    out = tools["remote.exec"].run({"server": "server-a", "command": "ls runs"})
    assert out.splitlines()[0] == f"$ ls runs  (on server-a, in {srv.work})"
    assert "exit 0 · " in out and "exp03.log" in out
    out = tools["remote.exec"].run({"server": "server-a", "command": "cat nope"})
    assert "exit 1 · " in out and "[stderr]" in out
    with pytest.raises(ToolError) as e:
        tools["remote.exec"].run({"server": "server-b", "command": "ls"})
    assert e.value.key == "tool.remote_unknown" and "server-a" in str(e.value)


# ---------------------------------------------------------------------- 연결
def test_unknown_host_key_and_wrong_key_are_refused(srv, tmp_path):
    other = asyncssh.generate_private_key("ssh-ed25519")
    tools, _ = tools_for(srv, tmp_path, srv.hub(known_hosts=srv.known_hosts(other)))
    with pytest.raises(ToolError) as e:
        tools["remote.exec"].run({"server": "server-a", "command": "ls"})
    assert e.value.key == "tool.remote_hostkey" and "ssh server-a" in str(e.value)
    tools, _ = tools_for(srv, tmp_path, srv.hub(client_keys=[other]))
    with pytest.raises(ToolError) as e:
        tools["remote.exec"].run({"server": "server-a", "command": "ls"})
    assert e.value.key == "tool.remote_auth"
    tools, _ = tools_for(srv, tmp_path, srv.hub(port=1))
    with pytest.raises(ToolError) as e:
        tools["remote.exec"].run({"server": "server-a", "command": "ls"})
    assert e.value.key == "tool.remote_unreachable" and "VPN" in str(e.value)


def test_reconnects_after_the_connection_drops(srv, tmp_path):
    hub = srv.hub()
    tools, _ = tools_for(srv, tmp_path, hub)
    tools["remote.exec"].run({"server": "server-a", "command": "pwd"})
    assert hub.connected("server-a")
    conn = hub._conns["server-a"]
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), hub._loop).result(5)
    hub._loop.call_soon_threadsafe(conn.close)
    asyncio.run_coroutine_threadsafe(conn.wait_closed(), hub._loop).result(5)
    out = tools["remote.exec"].run({"server": "server-a", "command": "pwd"})          # 끊긴 연결 대신 새로
    assert str(srv.work) in out and hub._conns["server-a"] is not conn
    hub.close()
    assert not hub.connected("server-a") or hub._loop is None


def test_no_servers_no_remote_tools(tmp_path):
    hctx = HandContext(places=make_ctx("linux").places, home=tmp_path, servers=[Server("", gpus=4)])
    assert remote.remote_tools(hctx) == [] and hctx.hub is None


def test_server_argument_is_limited_to_configured_aliases(srv, tmp_path):
    tools, _ = tools_for(srv, tmp_path)
    assert tools["remote.gpu"].params["properties"]["server"]["enum"] == ["server-a"]


# ---------------------------------------------------------------------- 공유 서버 규칙 (순수 코드)
def test_fair_rules():
    s = lambda ts, mem=0, util=0, others=0, mine=0: fair.GpuSample(ts, mem, util, others, mine)   # noqa: E731
    assert fair.state([s(0)], 0) == "checking" and fair.state([s(0), s(301)], 301) == "free"
    assert fair.state([s(0), s(100, util=50), s(301)], 301) == "checking"           # 끊기면 처음부터
    assert fair.free_for([s(0), s(100, util=50), s(150), s(301)], 400) == 250
    assert fair.state([s(0, mem=1100)], 0) == "busy" and fair.state([s(0, others=1)], 0) == "other"
    assert fair.state([s(0, mine=1, others=1)], 0) == "mine"
    assert fair.gpu_limit(datetime(2026, 11, 11, 14, 0)) == (1, True)                # 수요일 낮
    assert fair.gpu_limit(datetime(2026, 11, 11, 19, 0)) == (2, False)               # 저녁
    assert fair.gpu_limit(datetime(2026, 11, 14, 14, 0)) == (2, False)               # 토요일


# ---------------------------------------------------------------------- 정책
@pytest.mark.parametrize("command, verdict, rule", [
    ("ls runs", "allow", "기본:remote.exec:ls *"),
    ("git log -3", "allow", "기본:remote.exec:git log*"),
    ("python train.py", "ask", "기본:remote.exec:*"),
    ("ls runs | head", "ask", "코드:명령잇기"),
    ("rm -rf runs", "ask", "코드:서버지우기"),
    ("cat /home/kim/notes.txt", "deny", "코드:남의파일"),
    ("ls ~kim", "deny", "코드:남의파일"),
    ("du -sh /root", "deny", "코드:남의파일"),
    ("cat /home/saul_park/workspace/a.txt", "allow", "기본:remote.exec:cat *"),
    ("kill 4242", "deny", "코드:공유서버"),
    ("su kim", "deny", "코드:공유서버"),
    ("nvidia-smi", "ask", "기본:remote.exec:*"),                                    # 다른 사람 프로세스까지 보임
])
def test_remote_exec_policy(policy, command, verdict, rule):
    ctx = make_ctx("linux", remote_user=ME, remote_dirs={"server-a": "/home/saul_park/workspace"})
    d = check(policy, "linux", "remote.exec", ctx=ctx, server="server-a", command=command)
    assert (d.verdict, d.rule) == (verdict, rule), command


@pytest.mark.parametrize("path, verdict", [
    ("runs/exp03.log", "allow"), ("~/workspace/x.log", "allow"), ("/home/saul_park/workspace/x.log", "allow"),
    ("/data/shared/x.log", "deny"), ("/home/kim/x.log", "deny"), ("~kim/x.log", "deny"),
])
def test_remote_log_policy(policy, path, verdict):
    ctx = make_ctx("linux", remote_user=ME, remote_dirs={"server-a": "/home/saul_park/workspace"})
    assert check(policy, "linux", "remote.log", ctx=ctx, server="server-a", path=path).verdict == verdict, path


# ---------------------------------------------------------------------- 화면·프롬프트
def test_servers_command_and_prompt(srv, tmp_path, monkeypatch):
    home = tmp_path / "gh"
    ensure_home(home)
    (home / "servers.toml").write_text(f'[[server]]\nalias = "server-a"\ngpus = 4\nworkdir = "{srv.work}"\n',
                                       encoding="utf-8")
    cfg = load_config(home)
    out = io.StringIO()
    a = cli.App(cfg, cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux")
    try:
        assert {"remote.gpu", "remote.log", "remote.exec"} <= set(a.tools.names())
        prompt = a.system_prompt()
        assert f"Servers (shared GPU servers over SSH): server-a (4 GPUs, work folder {srv.work})" in prompt
        assert "No GPU servers are set up" not in prompt
        a.command("/servers")
        assert "서버 1대" in out.getvalue() and "server-a  연결 안 됨 · GPU 4장" in out.getvalue()
        a.hctx.hub.options.update({"port": srv.port, "username": ME, "known_hosts": srv.known_hosts(),
                                   "client_keys": [srv.client_key], "agent_path": None, "config": []})
        a.hctx.hub.hosts["server-a"] = "127.0.0.1"
        a.tools.get("remote.gpu").run({"server": "server-a"})
        a.command("/servers")
        text = out.getvalue()
        assert "server-a  연결됨 · GPU 4장" in text and "빔 0, 확인 중 1, 내 것 1, 사용 중 2" in text
    finally:
        a.close()
    ensure_home(tmp_path / "gh2")                                     # 기본 servers.toml: alias가 비어 있음
    b = cli.App(load_config(tmp_path / "gh2"), cli.Screen(ansi=False, out=io.StringIO()), llm=FakeLLM([]), os_name="linux")
    try:
        assert not any(n.startswith("remote.") for n in b.tools.names())
        assert "No GPU servers are set up yet (servers.toml)." in b.system_prompt()
    finally:
        b.close()
