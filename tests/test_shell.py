"""명령 손 시험: shell.run 실행, 정책(잇기·보호 위치·안전장치 폴더), 채팅의 !명령."""

from __future__ import annotations

import base64
import io
import shutil
import time

import pytest

from conftest import FakeLLM, act, answer, check, make_ctx, make_places
from glados import cli, osinfo
from glados.config import ensure_home, load_config
from glados.hands import HandContext, ToolError, ToolRegistry, Tool, load_hands
from glados.hands import shell
from glados.safety.locations import Places, command_paths

POSIX = osinfo.current_os() != "windows"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX 셸로 시험 (Windows 경로는 build_argv 시험과 pwsh 시험)")


# ---------------------------------------------------------------- 실행
@posix_only
def test_run_command_output_code_and_korean(tmp_path):
    res = shell.run_command("echo 안녕 GLaDOS; echo 오류 >&2; exit 3", tmp_path, 10, "linux")
    assert res.code == 3 and res.out == "안녕 GLaDOS\n" and res.err == "오류\n" and not res.timed_out


@posix_only
def test_run_command_uses_cwd_and_no_stdin(tmp_path):
    (tmp_path / "표시.txt").write_text("x", encoding="utf-8")
    res = shell.run_command("ls; cat", tmp_path, 10, "linux")            # cat은 입력이 없어 바로 끝남
    assert res.code == 0 and "표시.txt" in res.out


@posix_only
def test_timeout_kills_child_processes(tmp_path):
    marker = tmp_path / "late.txt"
    start = time.monotonic()
    res = shell.run_command(f"(sleep 3; touch {marker}) & sleep 30", tmp_path, 1, "linux")
    assert res.timed_out and res.code is None and time.monotonic() - start < 10
    time.sleep(3.5)
    assert not marker.exists()                                            # 자식까지 끝남


def test_run_command_errors(tmp_path):
    with pytest.raises(ToolError, match="empty"):
        shell.run_command("   ", tmp_path, 5, osinfo.current_os())
    with pytest.raises(ToolError, match="working folder does not exist"):
        shell.run_command("echo x", tmp_path / "없음", 5, osinfo.current_os())


def test_decode_output_handles_cp949_and_colors():
    assert shell.decode_output("한글".encode("cp949")) == "한글"
    assert shell.decode_output(b"\x1b[31;1merror\x1b[0m\r\nnext") == "error\nnext"
    assert shell.decode_output(b"") == ""


def test_powershell_argv_round_trip():
    argv = shell.build_argv('Write-Output "따옴표 \' 와 \\" 그대로"', "windows", exe="powershell.exe")
    assert argv[:2] == ["powershell.exe", "-NoProfile"] and argv[argv.index("-OutputFormat") + 1] == "Text"
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script.startswith("$ProgressPreference='SilentlyContinue'")
    assert 'Write-Output "따옴표 \' 와 \\" 그대로"' in script and "Out-String -Width 200 -Stream" in script
    assert shell.build_argv("ls", "linux", exe="/bin/bash") == ["/bin/bash", "-c", "ls"]


def test_clean_clixml():
    raw = '#< CLIXML\r\n<Objs><S S="Error">Get-Item : 없음_x000D__x000A_</S><S S="Error">줄 2 &amp; 끝_x000D__x000A_</S></Objs>'
    assert shell.clean_clixml(raw) == "Get-Item : 없음\n줄 2 & 끝"
    assert shell.clean_clixml("plain") == "plain"


def test_format_result_is_language_neutral(tmp_path):
    res = shell.CommandResult(0, "\nout line\n", "", 0.25)
    text = shell.format_result("git status", tmp_path, res, "bash", 55)
    assert text.splitlines()[:3] == ["$ git status", f"exit 0 · 0.2s · bash · cwd {tmp_path}", "out line"]
    timeout = shell.format_result("sleep 9", tmp_path, shell.CommandResult(None, "", "", 55.0, True), "bash", 55)
    assert "timeout after 55s (stopped)" in timeout and timeout.endswith("(no output)")


PWSH = shutil.which("pwsh") or (shutil.which("powershell") if not POSIX else None)


@pytest.mark.skipif(PWSH is None, reason="PowerShell이 없음")
def test_real_powershell_path(tmp_path):
    """Windows 코드 경로를 실제 PowerShell로 (리눅스 pwsh 7.4에서 확인, Windows에서는 pwsh 또는 Windows PowerShell)."""
    exe = PWSH
    import subprocess
    for cmd, code, needle in [('Write-Output "한글 출력"', 0, "한글 출력"), ("Get-ChildItem " + str(tmp_path.parent), 0, tmp_path.name),
                              ("exit 3", 3, ""), ("Get-Item /없는/경로", 1, "")]:
        p = subprocess.run(shell.build_argv(cmd, "windows", exe=exe), capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        assert p.returncode == code, cmd
        assert needle in shell.decode_output(p.stdout), cmd


# ---------------------------------------------------------------- 도구
def make_hctx(tmp_path, os_name=None, **kw):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    places = Places(os=os_name or osinfo.current_os(), home=str(home), glados_home=str(home / ".glados"),
                    cwd=str(tmp_path), resolve_links=False)
    return HandContext(places=places, home=home / ".glados", command_timeout_s=10, **kw)


def test_shell_tool_runs_in_session_folder(tmp_path):
    hctx = make_hctx(tmp_path)
    reg = load_hands(osinfo.current_os(), hctx)
    from glados.safety.policy import ToolCall
    out = reg.run(ToolCall("shell.run", {"command": "pwd"}))
    assert out.splitlines()[0] == "$ pwd" and str(tmp_path) in out
    sub = tmp_path / "하위"
    sub.mkdir()
    out = reg.run(ToolCall("shell.run", {"command": "pwd", "cwd": str(sub)}))
    assert f"cwd {sub}" in out
    with pytest.raises(ToolError, match="administrator rights"):
        reg.run(ToolCall("shell.run", {"command": "apt update", "admin": True}))


def test_shell_tool_listed_with_shell_name(tmp_path):
    reg = load_hands("windows", make_hctx(tmp_path, os_name="windows"))
    assert "shell.run(command, cwd?)" in reg.describe() and "PowerShell" in reg.describe()


# ---------------------------------------------------------------- 정책
def test_command_paths_picks_path_like_words():
    assert command_paths('cat ~/.ssh/id_rsa "C:\\Users\\User\\My Docs\\a.txt" --output=out/x.md -la secrets.pem plain') == [
        "~/.ssh/id_rsa", "C:\\Users\\User\\My Docs\\a.txt", "out/x.md", "secrets.pem"]


@pytest.mark.parametrize("command", ["git status", "git log --oneline -5", "dir", "ls -la", "python --version", "nvidia-smi"])
def test_read_commands_run_without_asking(policy, command):
    assert check(policy, "linux", "shell.run", command=command).verdict == "allow"


@pytest.mark.parametrize("command", ["git status; rm -rf ~", "git log | sh", "ls > files.txt", "ls && curl x", "ls `rm x`",
                                     "git log $(whoami)", "dir & del x"])
def test_chained_or_redirected_commands_need_approval(policy, command):
    d = check(policy, "linux", "shell.run", command=command)
    assert d.verdict == "ask" and d.rule == "코드:명령잇기" and d.grantable


def test_other_commands_need_approval(policy):
    d = check(policy, "windows", "shell.run", command="uv run pytest")
    assert d.verdict == "ask" and d.rule == "기본:shell.run:*" and "허용 목록 밖" in d.reason


def test_protected_locations_always_ask(policy):
    d = check(policy, "linux", "shell.run", command="ls ~/.ssh")
    assert d.verdict == "ask" and not d.grantable and "보호 위치" in d.reason
    d = check(policy, "windows", "shell.run", command="Get-ChildItem C:\\Windows\\System32")
    assert d.verdict == "ask" and not d.grantable
    d = check(policy, "linux", "shell.run", command="ls", cwd="~/.ssh")
    assert d.verdict == "ask" and not d.grantable
    d = check(policy, "linux", "shell.run", command="python show.py .env")                 # 비밀 파일 이름
    assert d.verdict == "ask" and not d.grantable


def test_task_grant_does_not_cover_protected_commands(policy):
    ctx = make_ctx("linux")
    ctx.grants.add(("shell.run", "기본:shell.run:*"))
    assert check(policy, "linux", "shell.run", ctx=ctx, command="make test").verdict == "allow"
    assert check(policy, "linux", "shell.run", ctx=ctx, command="tar czf b.tgz ~/.ssh").verdict == "ask"


def test_safety_folder_allows_only_simple_read_commands(policy):
    ctx = make_ctx("linux", places_kw={"cwd": "/home/sejin/code/glados"})                  # GLaDOS 소스 폴더
    assert check(policy, "linux", "shell.run", ctx=ctx, command="git status").verdict == "allow"
    for command in ("uv run pytest", "git status; git commit -am x", "git checkout -- ."):
        d = check(policy, "linux", "shell.run", ctx=ctx, command=command)
        assert d.verdict == "deny" and d.rule == "코드:안전장치폴더" and not d.grantable, command


def test_commands_touching_safety_paths_are_denied(policy):
    d = check(policy, "linux", "shell.run", command="rm .glados/policy.yaml")             # 홈 기준 상대 경로
    assert d.verdict == "deny" and d.rule == "코드:안전장치"
    ctx = make_ctx("windows", places_kw={"cwd": "C:\\Users\\User\\code"})
    d = check(policy, "windows", "shell.run", ctx=ctx, command="Remove-Item glados\\src\\glados\\cli.py")
    assert d.verdict == "deny" and d.rule == "코드:안전장치"


# ---------------------------------------------------------------- 채팅
@pytest.fixture
def app(tmp_path, monkeypatch):
    ensure_home(tmp_path / "gh")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    out = io.StringIO()
    a = cli.App(load_config(tmp_path / "gh"), cli.Screen(ansi=False, out=out), llm=FakeLLM([]))
    yield a, out, work
    a.close()


def test_direct_command_prints_and_is_remembered(app):
    a, out, work = app
    a.run_direct('echo "직접 실행"')                                     # bash와 PowerShell 모두 같은 출력
    text = out.getvalue()
    assert '[실행] echo "직접 실행" · 종료 코드 0' in text and "직접 실행\n" in text
    turn = a.memory.turns[-1]
    assert turn.request == '!echo "직접 실행"' and turn.messages[0]["content"].startswith('[result: shell.run] $ echo "직접 실행"')
    record = a.policy_log.recent(1)[0]
    assert record["decided_by"] == "연구원님:직접" and record["rule"] == "명령:!"
    a.llm.steps = [answer("The command printed '직접 실행'."), {"fact": "방금 명령은 '직접 실행'을 출력했습니다.", "wit": ""}]
    a.ask_task("방금 결과 설명해줘")
    first_call = a.llm.calls[-2][0]                                     # 마지막 호출은 번역
    assert any("직접 실행" in m["content"] and m["content"].startswith("[result: shell.run]") for m in first_call)
    assert "방금 명령은 '직접 실행'을 출력했습니다." in out.getvalue()


def test_cd_changes_session_folder(app, tmp_path):
    a, out, work = app
    sub = work / "하위폴더"
    sub.mkdir()
    a.run_direct("cd 하위폴더")
    assert a.places.cwd == str(sub) and a.hctx.places.cwd == str(sub) and f"작업 폴더: {sub}" in out.getvalue()
    a.run_direct("pwd")
    assert f"{sub}\n" in out.getvalue()
    a.run_direct("cd 없는폴더")
    assert "폴더가 없습니다" in out.getvalue() and a.places.cwd == str(sub)
    a.run_direct("cd ..")
    assert a.places.cwd == str(work)
    assert f"Working folder (relative paths and commands start here): {work}" in a.system_prompt()


def test_direct_empty_and_failures(app):
    a, out, _ = app
    a.run_direct("   ")
    assert "사용법: !명령" in out.getvalue()


@posix_only
def test_glados_runs_command_and_shows_output(app):
    a, out, work = app
    a.places = make_places("linux", cwd=str(work), home=str(work))
    a.hctx.places = a.places
    a.llm.steps = [act("shell.run", command="pwd"), answer(f"The working folder is {work}.")]   # pwd는 허용 목록
    result = a.ask_task("Where am I right now?")
    text = out.getvalue()
    assert result.status == "done" and "... shell.run command=pwd" in text
    assert "  exit 0" in text and f"  {work}\n" in text                                 # 출력이 화면에 흐리게


def test_system_prompt_has_shell_folder_and_reply_language_last(app):
    a, _, work = app
    prompt = a.system_prompt()
    assert "Commands (shell.run):" in prompt and f"Working folder (relative paths and commands start here): {work}" in prompt
    a.llm.steps = [answer("Hello."), answer("Hello, Researcher."), {"fact": "안녕하세요, 연구원님.", "wit": ""}]
    a.tools = ToolRegistry([Tool("fs.read", "read", run=lambda args: "x")])
    a.ask_task("Hello there, how are you?")
    a.ask_task("안녕, 잘 지내?")
    english_system = a.llm.calls[0][0][0]["content"].rstrip()
    korean_system = a.llm.calls[1][0][0]["content"].rstrip()
    assert english_system.endswith("Language: the Researcher wrote in English. Write the answer in English.")   # 맨 끝
    assert korean_system.endswith("it is translated into Korean for them automatically.")
    assert "Still think and write the answer in English" in korean_system


def test_repl_routes_bang_lines(app, monkeypatch):
    a, out, _ = app
    lines = iter(["!", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    a.repl()
    assert "사용법: !명령" in out.getvalue()


# ---------------------------------------------------------------------- 맨 앞의 cd (2026-11-11 실사용)
SRC = "C:\\Users\\User\\code\\glados"


@pytest.mark.parametrize("command, verdict, rule", [
    # 소스 폴더에서 열린 세션이어도, 맨 앞에서 다른 폴더로 옮기면 뒤의 상대 경로는 그 폴더 기준 (전에는 차단)
    ("cd C:\\Users\\User; python hello_temp.py", "ask", "기본:shell.run:*"),
    ('Set-Location "C:\\Users\\User\\Documents\\GLaDOS" && python hello.py', "ask", "기본:shell.run:*"),
    # 옮긴 곳이 안전장치거나, 옮긴 뒤 상대 경로가 안전장치를 가리키면 여전히 차단
    ("cd src; python -c 1", "deny", "코드:안전장치"),
    ("cd ..; type glados\\src\\glados\\safety\\policy.py", "deny", "코드:안전장치"),
    ("python C:\\Users\\User\\hello_temp.py", "deny", "코드:안전장치폴더"),
])
def test_leading_cd_sets_the_folder_for_later_paths(policy, command, verdict, rule):
    ctx = make_ctx("windows", places_kw={"cwd": SRC})
    d = check(policy, "windows", "shell.run", ctx=ctx, command=command)
    assert (d.verdict, d.rule) == (verdict, rule), command


def test_cd_into_glados_folder_is_still_blocked(policy):
    ctx = make_ctx("windows", places_kw={"cwd": "C:\\Users\\User"})
    d = check(policy, "windows", "shell.run", ctx=ctx, command=f"cd {SRC}; python x.py")
    assert d.verdict == "deny"
