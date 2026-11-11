"""명령 손: GLaDOS가 부르는 shell.run, 연구원님이 채팅에 치는 '!명령'.

- Windows는 PowerShell(pwsh가 있으면 pwsh, 없으면 Windows PowerShell), 맥·리눅스는 연구원님의 셸(bash·zsh, 없으면 sh)
- 명령마다 새 셸. 입력은 받지 않는다 (편집기·대화형 프로그램은 안 됨)
- PowerShell에는 명령을 -EncodedCommand(UTF-16 base64)로 넘긴다. 따옴표가 깨지지 않고 한글 출력은 UTF-8로 받는다.
  콘솔 창이 없으면 표(Get-ChildItem 등)가 빈 줄로 나오므로 Out-String -Width 200으로 감싼다.
  cmdlet 오류는 종료 코드 1, 프로그램 오류는 그 프로그램의 종료 코드 (리눅스 pwsh 7.4로 확인)
- 출력은 utf-8로 읽고, 안 되면 cp949(한국어 Windows의 옛 프로그램) → 그 밖
- 시간 제한을 넘거나 Ctrl+C를 누르면 명령과 그 자식 프로세스까지 끝낸다
- 정책 검사는 엔진이 한다 (shell.run). '!명령'은 연구원님이 직접 치는 것이라 검사 없이 돌리고 기록만 남긴다
- 도구 설명과 오류 글은 영어 (모델이 영어로 생각함). 오류는 키도 담아 화면에서는 설정한 언어로
- 관리자 권한 실행(승인 + OS 확인 창)은 아직 없다 (v1)
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glados.hands import HandContext, Tool, tool_error

SHELL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "cwd": {"type": "string"},
    },
    "required": ["command"],
}

POSIX_SHELLS = {"bash", "zsh", "sh", "dash", "ksh"}
# 한 줄로 둔다 (오류 메시지의 줄 번호가 연구원님 명령 기준에 가깝게)
PS_PRELUDE = (
    "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
    "$OutputEncoding=[System.Text.Encoding]::UTF8; if ($PSStyle) { $PSStyle.OutputRendering='PlainText' }; "
    "$__glados_errors = $Error.Count\n"
)
PS_EPILOGUE = (
    "\n} | Out-String -Width 200 -Stream\n"
    "if ($LASTEXITCODE) { exit $LASTEXITCODE } elseif ($Error.Count -gt $__glados_errors) { exit 1 }\n"
)
CLIXML_STRING = re.compile(r'<S S="Error">(.*?)</S>', re.DOTALL)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


_RUNNING: set[subprocess.Popen] = set()        # 지금 돌고 있는 명령 (Ctrl+C 때 끝내려고)
# TUI가 켠다 (Windows): 명령을 숨은 콘솔에서 돌린다. 같은 콘솔을 쓰면 명령(PowerShell 등)이 콘솔 입력 설정을
# 바꾸거나, 강제로 끝낼 때 되돌리지 못해 TUI의 키 입력이 망가질 수 있다. 출력은 어차피 파이프로 받는다
DETACH_CONSOLE = False
_LOCK = threading.Lock()


@dataclass
class CommandResult:
    code: int | None          # 종료 코드 (시간 초과·중단이면 None)
    out: str
    err: str
    seconds: float
    timed_out: bool = False


# ---------------------------------------------------------------------- 셸 고르기
def powershell_exe() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def posix_shell(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    shell = env.get("SHELL", "")
    if shell and Path(shell).name in POSIX_SHELLS and Path(shell).exists():
        return shell
    return shutil.which("bash") or "/bin/sh"


def shell_label(os_name: str) -> str:
    """시스템 프롬프트와 결과에 쓰는 셸 이름."""
    if os_name == "windows":
        return "PowerShell"
    return Path(posix_shell()).name


def build_argv(command: str, os_name: str, exe: str | None = None) -> list[str]:
    if os_name == "windows":
        script = PS_PRELUDE + "& {\n" + command + PS_EPILOGUE
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [exe or powershell_exe(), "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
                "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    return [exe or posix_shell(), "-c", command]


# ---------------------------------------------------------------------- 실행
def decode_output(data: bytes) -> str:
    """utf-8, 안 되면 cp949(한국어 Windows의 옛 프로그램). 색 표시(ANSI)는 뺀다."""
    if not data:
        return ""
    for enc in ("utf-8", "cp949"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    return ANSI.sub("", text).replace("\r\n", "\n")


def clean_clixml(text: str) -> str:
    """PowerShell이 오류를 CLIXML로 내보내는 경우 사람이 읽는 글로."""
    if not text.lstrip().startswith("#< CLIXML"):
        return text
    parts = [m.replace("_x000D__x000A_", "\n").replace("_x000A_", "\n") for m in CLIXML_STRING.findall(text)]
    cleaned = "".join(parts).strip()
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        cleaned = cleaned.replace(a, b)
    return cleaned


def _kill_tree(proc: subprocess.Popen, os_name: str) -> None:
    try:
        if os_name == "windows":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def kill_running(os_name: str) -> None:
    """돌고 있는 명령을 모두 끝낸다 (작업 중 Ctrl+C. GLaDOS의 명령은 다른 스레드에서 돌아서 따로 끝내야 함)."""
    with _LOCK:
        procs = list(_RUNNING)
    for proc in procs:
        if proc.poll() is None:
            _kill_tree(proc, os_name)


def run_command(command: str, cwd: str | Path, timeout_s: float, os_name: str) -> CommandResult:
    """명령 하나를 실행하고 결과를 돌려준다. 실행 자체를 못 하면 ToolError."""
    if not command.strip():
        raise tool_error("shell.empty")
    if not Path(cwd).is_dir():
        raise tool_error("shell.no_cwd", path=str(cwd))
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    kwargs: dict[str, Any] = {}
    if os_name != "windows":
        kwargs["start_new_session"] = True          # 시간 초과 때 자식까지 한 번에 끝내려고 (Windows는 taskkill /T)
    elif DETACH_CONSOLE and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            build_argv(command, os_name), cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except OSError as e:
        raise tool_error("shell.cannot_run", detail=str(e)) from None
    with _LOCK:
        _RUNNING.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, os_name)
        out, err = proc.communicate()
        return CommandResult(None, decode_output(out), clean_clixml(decode_output(err)),
                             time.monotonic() - start, timed_out=True)
    except KeyboardInterrupt:
        _kill_tree(proc, os_name)
        proc.communicate()
        raise
    finally:
        with _LOCK:
            _RUNNING.discard(proc)
    return CommandResult(proc.returncode, decode_output(out), clean_clixml(decode_output(err)), time.monotonic() - start)


def format_result(command: str, cwd: str | Path, res: CommandResult, shell: str, timeout_s: float) -> str:
    """모델이 보는 결과. 언어에 기대지 않는 꼴 (화면에도 그대로 보여 줄 수 있게)."""
    status = f"timeout after {timeout_s:.0f}s (stopped)" if res.timed_out else f"exit {res.code}"
    lines = [f"$ {command}", f"{status} · {res.seconds:.1f}s · {shell} · cwd {cwd}"]
    out, err = res.out.rstrip().lstrip("\n"), res.err.rstrip().lstrip("\n")
    if out:
        lines.append(out)
    if err:
        lines += ["[stderr]", err]
    if not out and not err:
        lines.append("(no output)")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 도구
def work_dir(hctx: HandContext, raw: Any = None) -> Path:
    """명령을 실행할 폴더: 인자로 받은 폴더, 없으면 이 세션의 작업 폴더."""
    from glados.safety.locations import normalize

    if raw:
        return Path(normalize(str(raw), hctx.places))
    return Path(hctx.places.cwd or hctx.places.home)


def run_tool(args: dict[str, Any], hctx: HandContext) -> str:
    if args.get("admin"):
        raise tool_error("tool.admin_not_yet")
    command = str(args.get("command") or "").strip()
    cwd = work_dir(hctx, args.get("cwd"))
    res = run_command(command, cwd, hctx.command_timeout_s, hctx.places.os)
    return format_result(command, cwd, res, shell_label(hctx.places.os), hctx.command_timeout_s)


def shell_tools(hctx: HandContext) -> list[Tool]:
    shell = shell_label(hctx.places.os)
    return [
        Tool(
            "shell.run",
            f"Run one command in {shell}. cwd: folder to run in (default: the working folder). Returns the exit code "
            "and output. Read-only commands (git status, version checks, ...) run at once; other commands, and "
            "anything chained or redirected with ; && | >, need the Researcher's approval",
            SHELL_PARAMS,
            lambda a: run_tool(a, hctx),
            timeout_s=hctx.command_timeout_s + 5,
        ),
    ]
