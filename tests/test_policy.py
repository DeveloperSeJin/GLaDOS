"""정책 엔진 시험. 모델 없이 순수 코드로 돈다 (설계 원칙 1)."""

from __future__ import annotations

import pytest

from conftest import ALL_TOOLS, base_layers, check, make_ctx
from glados.safety.policy import Policy, PolicyContext, ToolCall


def verdict(policy, os_name, tool, ctx=None, **args):
    return check(policy, os_name, tool, ctx=ctx, **args).verdict


# ---------------------------------------------------------------- 등록·규칙 없음
def test_unregistered_tool_denied(policy):
    ctx = make_ctx("linux", registered_tools=frozenset({"fs.read"}))
    d = policy.check(ToolCall("fs.write_new", {"path": "/tmp/a.txt"}), ctx)
    assert d.verdict == "deny" and d.rule == "코드:미등록도구" and not d.grantable


def test_unregistered_mcp_tool_denied(policy):
    assert verdict(policy, "linux", "mcp.unknown.run", command="x") == "deny"


def test_registered_tool_without_rules_denied(policy):
    ctx = make_ctx("linux", registered_tools=ALL_TOOLS | {"app.open"})
    d = policy.check(ToolCall("app.open", {"text": "Notes"}), ctx)
    assert d.verdict == "deny" and d.rule.startswith("없음")


# ---------------------------------------------------------------- Windows 경로
@pytest.mark.parametrize(
    "path, expected",
    [
        ("C:\\Users\\User\\Documents\\보고서.docx", "allow"),
        ("C:\\Users\\User\\.ssh\\config", "ask"),
        ("C:\\Users\\User\\AppData\\Roaming\\Microsoft\\x.json", "ask"),
        ("C:\\Windows\\System32\\drivers\\etc\\hosts", "ask"),
        ("C:\\Program Files\\Ollama\\ollama.exe", "ask"),
        ("C:\\ProgramData\\x.cfg", "ask"),
        ("C:\\USERS\\USER\\.SSH\\CONFIG", "ask"),                           # 대소문자 무시
        ("C:/Users/User/AppData/Local/Temp/x.txt", "ask"),                  # / 구분자
        ("C:\\Users\\User\\Documents\\..\\.ssh\\id_ed25519", "ask"),         # .. 풀기
        ("%APPDATA%\\Code\\User\\settings.json", "ask"),                    # 환경 변수
        ("~\\.aws\\credentials", "ask"),                                    # ~
        (".ssh/config", "ask"),                                             # 상대 경로는 홈 기준
        ("C:\\Users\\User\\NTUSER.DAT", "ask"),
        ("C:\\Users\\User\\Documents\\PowerShell\\Microsoft.PowerShell_profile.ps1", "ask"),
        ("D:\\data\\results.csv", "allow"),
    ],
)
def test_windows_read(policy, path, expected):
    assert verdict(policy, "windows", "fs.read", path=path) == expected


# ---------------------------------------------------------------- 리눅스·맥 경로
@pytest.mark.parametrize(
    "os_name, path, expected",
    [
        ("linux", "/home/sejin/research/notes.md", "allow"),
        ("linux", "/etc/passwd", "ask"),
        ("linux", "~/.ssh/id_ed25519", "ask"),
        ("linux", "~/.local/share/keyrings/login.keyring", "ask"),
        ("linux", "~/.config/google-chrome/Default/Cookies", "ask"),
        ("linux", "/var/log/syslog", "ask"),
        ("linux", "/tmp/glados-test.txt", "allow"),
        ("mac", "~/Library/Keychains/login.keychain-db", "ask"),
        ("mac", "/users/sejin/.SSH/id_rsa", "ask"),                         # 맥도 대소문자 무시
        ("mac", "/System/Library/CoreServices/x.plist", "ask"),
        ("mac", "/Users/sejin/Documents/계약서.pdf", "allow"),
    ],
)
def test_posix_read(policy, os_name, path, expected):
    assert verdict(policy, os_name, "fs.read", path=path) == expected


@pytest.mark.parametrize("path", ["/home/sejin/proj/.env", "/home/sejin/proj/.env.local", "/home/sejin/certs/server.pem"])
def test_secret_names_anywhere_ask(policy, path):
    d = check(policy, "linux", "fs.read", path=path)
    assert d.verdict == "ask" and "보호 위치" in d.reason


def test_search_roots_protected_asks(policy):
    assert verdict(policy, "linux", "fs.search", query="key", roots=["~/Documents", "~/.ssh"]) == "ask"


def test_search_without_roots_allowed(policy):
    assert verdict(policy, "linux", "fs.search", query="계약서") == "allow"


def test_extra_protected_korean_path(policy):
    ctx = make_ctx("linux", places_kw={"extra_protected": ("~/연구/비공개",)})
    assert verdict(policy, "linux", "fs.read", ctx=ctx, path="~/연구/비공개/메모.txt") == "ask"
    assert verdict(policy, "linux", "fs.read", ctx=ctx, path="~/연구/공개/메모.txt") == "allow"


# ---------------------------------------------------------------- 쓰기
def test_write_new_normal_allowed(policy):
    assert verdict(policy, "windows", "fs.write_new", path="C:\\Users\\User\\Documents\\요약.md") == "allow"


def test_write_new_autostart_asks(policy):
    assert verdict(policy, "linux", "fs.write_new", path="~/.config/autostart/x.desktop") == "ask"


def test_edit_shell_profile_asks(policy):
    assert verdict(policy, "linux", "fs.edit", path="~/.bashrc") == "ask"


def test_trash_asks(policy):
    assert verdict(policy, "windows", "fs.trash", path="C:\\Users\\User\\Downloads\\old.zip") == "ask"


# ---------------------------------------------------------------- GLaDOS 안전장치
@pytest.mark.parametrize(
    "os_name, tool, path",
    [
        ("windows", "fs.write_new", "C:\\Users\\User\\code\\.glados\\policy.yaml"),
        ("windows", "fs.edit", "C:\\Users\\User\\code\\.glados\\folders.toml"),
        ("linux", "fs.trash", "~/.glados/logs/policy.jsonl"),
        ("linux", "fs.write_new", "~/.glados/checkpoints/x.bak"),
        ("windows", "fs.edit", "C:\\Users\\User\\code\\glados\\src\\glados\\safety\\policy.py"),
        ("linux", "fs.write_new", "/home/sejin/exp/repo/.glados/policy.yaml"),
        ("mac", "fs.edit", "/Users/sejin/.GLADOS/glados.toml"),
    ],
)
def test_safety_writes_denied(policy, os_name, tool, path):
    d = check(policy, os_name, tool, path=path)
    assert d.verdict == "deny" and d.rule == "코드:안전장치" and not d.grantable


def test_safety_denied_even_with_grant(policy):
    ctx = make_ctx("linux", grants={("fs.edit", "기본:fs.edit:*")})
    assert verdict(policy, "linux", "fs.edit", ctx=ctx, path="~/.glados/policy.yaml") == "deny"


def test_reading_own_logs_allowed(policy):
    assert verdict(policy, "linux", "fs.read", path="~/.glados/logs/tasks.jsonl") == "allow"


def test_user_layer_cannot_unlock_safety():
    p = Policy(base_layers() + [("연구원님", {"fs.edit": {"*": "allow"}})])
    assert verdict(p, "linux", "fs.edit", path="~/.glados/policy.yaml") == "deny"
    assert verdict(p, "linux", "fs.edit", path="~/notes.md") == "allow"


# ---------------------------------------------------------------- 셸 명령
def test_shell_allowlisted(policy):
    assert verdict(policy, "windows", "shell.run", command="git status") == "allow"


def test_shell_unknown_asks(policy):
    d = check(policy, "linux", "shell.run", command="rm -rf build")
    assert d.verdict == "ask" and d.grantable


def test_shell_admin_flag_asks_and_not_grantable(policy):
    ctx = make_ctx("windows", grants={("shell.run", "코드:관리자권한"), ("shell.run", "기본:shell.run:*")})
    d = check(policy, "windows", "shell.run", ctx=ctx, command="winget install --id Git.Git -e", admin=True)
    assert d.verdict == "ask" and not d.grantable


def test_shell_admin_flag_even_for_allowlisted_command(policy):
    assert verdict(policy, "linux", "shell.run", command="git status", admin=True) == "ask"


@pytest.mark.parametrize(
    "command",
    ["sudo apt install ripgrep", "pkexec systemctl restart ollama", "Start-Process powershell -Verb RunAs", "ls; sudo reboot"],
)
def test_shell_elevation_inside_command_denied(policy, command):
    d = check(policy, "linux", "shell.run", command=command)
    assert d.verdict == "deny" and d.rule == "코드:관리자표시"


@pytest.mark.parametrize(
    "os_name, command",
    [
        ("windows", "del C:\\Users\\User\\code\\.glados\\policy.yaml"),
        ("windows", "type C:/Users/User/code/.glados/glados.toml"),
        ("linux", "echo '' > ~/project/policy.yaml"),
        ("linux", "rm -rf /home/sejin/code/glados/tests"),
    ],
)
def test_shell_touching_safety_denied(policy, os_name, command):
    d = check(policy, os_name, "shell.run", command=command)
    assert d.verdict == "deny" and d.rule == "코드:안전장치"


def test_task_grant_turns_ask_into_allow(policy):
    ctx = make_ctx("linux")
    first = check(policy, "linux", "shell.run", ctx=ctx, command="make test")
    assert first.verdict == "ask"
    ctx.grants.add(("shell.run", first.rule))
    second = check(policy, "linux", "shell.run", ctx=ctx, command="make lint")
    assert second.verdict == "allow" and "이 작업 동안" in second.reason


# ---------------------------------------------------------------- 공유 서버
def test_remote_readonly_allowed(policy):
    assert verdict(policy, "linux", "remote.exec", command="nvidia-smi --query-gpu=memory.used --format=csv") == "allow"


def test_remote_unknown_denied(policy):
    assert verdict(policy, "linux", "remote.exec", command="rm -rf ~/workspace/old") == "deny"


def test_remote_sudo_denied_even_if_user_allows():
    p = Policy(base_layers() + [("연구원님", {"remote.exec": {"*": "allow"}})])
    d = p.check(ToolCall("remote.exec", {"command": "sudo kill -9 1234"}), make_ctx("linux"))
    assert d.verdict == "deny" and d.rule == "코드:공유서버"


# ---------------------------------------------------------------- 음성·실험 단계
def test_voice_write_denied(policy):
    ctx = make_ctx("linux", source="voice")
    assert verdict(policy, "linux", "fs.write_new", ctx=ctx, path="~/a.md") == "deny"


def test_voice_gpu_allowed(policy):
    ctx = make_ctx("linux", source="voice")
    assert verdict(policy, "linux", "remote.gpu", ctx=ctx, server="server-a") == "allow"


@pytest.mark.parametrize("stage", ["hypothesis", "design"])
def test_readonly_stages_block_writes(policy, stage):
    ctx = make_ctx("linux", stage=stage)
    assert verdict(policy, "linux", "fs.write_new", ctx=ctx, path="~/exp/plan.md") == "deny"
    assert verdict(policy, "linux", "fs.read", ctx=ctx, path="~/exp/data.csv") == "allow"


def test_implement_stage_allows_writes(policy):
    ctx = make_ctx("linux", stage="implement")
    assert verdict(policy, "linux", "fs.write_new", ctx=ctx, path="~/exp/train.py") == "allow"


# ---------------------------------------------------------------- 층 겹치기
def test_user_layer_overrides_default():
    p = Policy(base_layers() + [("연구원님", {"shell.run": {"make *": "allow"}})])
    assert verdict(p, "linux", "shell.run", command="make test") == "allow"
    assert verdict(p, "linux", "shell.run", command="cmake ..") == "ask"


def test_last_match_wins_within_layer():
    p = Policy([("시험", {"fs.read": {"*": "allow", "*.log": "deny"}})])
    assert verdict(p, "linux", "fs.read", path="/tmp/run.log") == "deny"
    assert verdict(p, "linux", "fs.read", path="/tmp/run.txt") == "allow"


def test_no_matching_rule_denied():
    p = Policy([("시험", {"fs.read": {"*.txt": "allow"}})])
    d = p.check(ToolCall("fs.read", {"path": "/tmp/a.md"}), make_ctx("linux"))
    assert d.verdict == "deny" and "맞는 정책 규칙이 없어" in d.reason


def test_invalid_verdict_denied():
    p = Policy([("시험", {"fs.read": {"*": "maybe"}})])
    assert verdict(p, "linux", "fs.read", path="/tmp/a.txt") == "deny"


def test_user_can_relax_protected_reads():
    p = Policy(base_layers() + [("연구원님", {"fs.read": {"@protected": "allow"}})])
    assert verdict(p, "linux", "fs.read", path="~/.ssh/config") == "allow"


def test_path_pattern_with_tilde():
    p = Policy([("시험", {"fs.read": {"*": "ask", "~/research/*": "allow"}})])
    assert verdict(p, "windows", "fs.read", path="C:\\Users\\User\\research\\a.csv") == "allow"
    assert verdict(p, "windows", "fs.read", path="C:\\Users\\User\\other\\a.csv") == "ask"


def test_web_search_allowed(policy):
    assert verdict(policy, "linux", "web.search", query="LLM factuality metrics 2026") == "allow"


def test_audit_rule_names_layer(policy):
    d = check(policy, "linux", "fs.read", path="/tmp/a.txt")
    assert d.rule == "기본:fs.read:*"
