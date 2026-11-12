"""안전 정책 엔진.

모든 도구 호출은 실행 직전에 check()를 거친다. 모델이 건너뛸 방법은 없다.
판정: allow(허용) / ask(승인) / deny(차단) / wait(대기)

순서
1. 코드에 박힌 규칙 (policy.yaml로 풀 수 없음)
   - 등록되지 않은 도구 -> 차단
   - 실험 가설·설계 단계의 쓰기·실행 -> 차단
   - 음성 요청은 정해진 도구만
   - GLaDOS 안전장치에 쓰기·삭제 -> 차단 (승인으로도 못 풂)
   - 셸 명령: 관리자 권한은 admin 표시로만 요청 -> 승인 (작업 동안 승인 불가)
   - 공유 서버 sudo -> 차단, 프로세스·시스템 명령(kill, reboot ...) -> 차단, 다른 사람 폴더 -> 차단
2. policy.yaml 층 (기본 -> 연구원님 -> 서버 -> 저장소). 마지막에 맞는 규칙이 이긴다. 아무것도 안 맞으면 차단
3. 코드 규칙으로 더 보는 도구 (층의 판정이 허용이어도 승인으로 올림, 매번 승인)
   - shell.run: 잇거나 돌리는 명령, 보호 위치, 환경 변수 읽기(API 키가 있을 수 있음)
   - web.search·web.fetch (M3): 밖으로 보낼 글에 개인 정보·이 컴퓨터의 파일 내용(safety/privacy.py),
     읽을 주소가 이 컴퓨터나 내부 네트워크
   - remote.exec: 지우기·옮기기·덮어쓰기, 백그라운드로 띄우기(nohup, tmux new, & ...), 잇기
   - remote.run·remote.stop (M5): 매번 승인 (연구원님 결정: 실행마다 묻는다. "이 작업 동안" 없음)
   - memory.note (M6): 매번 승인 (연구원님 결정: MEMORY.md에 남길 글을 보고 정한다).
     비밀번호·PIN·카드 번호·키·토큰·주민등록번호가 든 글은 차단 (M6.2, 승인으로도 못 풂: 평문 파일이고 매 작업 모델에 들어감)
4. "이 작업 동안" 승인을 받은 규칙이면 ask -> allow
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from glados.i18n import localized
from glados.safety import privacy
from glados.safety.locations import (
    Location, Places, classify, command_cwd, command_locations, fold, mentions_safety, normalize,
)

Verdict = Literal["allow", "ask", "deny", "wait"]
VERDICTS = ("allow", "ask", "deny", "wait")

# 경로를 받는 도구와 그 성격
PATH_TOOLS: dict[str, str] = {
    "fs.search": "read",
    "fs.inspect": "read",
    "fs.read": "read",
    "fs.write_new": "write",
    "fs.edit": "write",
    "fs.trash": "write",
}
# 쓰기·실행 도구 (실험 가설·설계 단계에서 막음)
WRITE_OR_EXEC = {"fs.write_new", "fs.edit", "fs.trash", "shell.run", "remote.exec", "remote.edit", "remote.run"}
READONLY_STAGES = {"hypothesis", "design"}
# 음성 요청이 부를 수 있는 도구 (6단계에서 명령 목록과 함께 확정)
VOICE_TOOLS = {"ask.user", "remote.gpu", "remote.log"}

ADMIN_WORDS = re.compile(r"(^|[\s;&|(])(sudo|pkexec|doas|runas|gsudo)(\s|$)|-verb\s+runas", re.IGNORECASE)
REMOTE_SUDO = re.compile(r"(^|[\s;&|(`])(sudo|su|doas|pkexec)(\s|$)")
# 공유 서버에서 프로세스·시스템을 건드리는 명령: 막는다. 내 실행 멈추기는 remote.stop으로만 (M5)
REMOTE_PROC = re.compile(r"(^|[\s;&|(`])(kill|pkill|killall|reboot|shutdown|halt|poweroff|systemctl|renice)(\s|$)"
                         r"|\btmux\s+(?:\S+\s+)*kill-(?:session|server|window|pane)\b")
# 다른 사람의 폴더: /home/<다른 사람>, ~<다른 사람>, /root
OTHERS_PATH = re.compile(r"(?<![\w/.~-])(?:/home/([A-Za-z0-9._-]+)|/root(?![\w.-])|~([A-Za-z0-9._-]+))")
REMOTE_TOOLS = {"remote.exec", "remote.log", "remote.run"}
# 서버에 오래 도는 작업을 띄우는 명령: 학습은 remote.run으로 (GPU 확인, 기록, 알림). 매번 승인
REMOTE_BACKGROUND = re.compile(r"(^|[\s;&|(`])(nohup|setsid|disown|screen|at|batch|tmux\s+(?:new|new-session|send|send-keys|neww|new-window|split|split-window))(\s|$)"
                               r"|(?<![&>|])&(?![&>])")
# 서버에서 지우거나 옮기거나 덮어쓰는 명령: 매번 승인 (되돌릴 수 없음, "이 작업 동안" 안 됨)
REMOTE_DESTRUCTIVE = re.compile(r"(^|[\s;&|(`])(rm|rmdir|mv|dd|mkfs\S*|truncate|shred|chmod|chown|git\s+(?:reset|clean|push|checkout))(\s|$)"
                                r"|>(?!&)")
# 명령을 잇거나(; && || |), 출력을 돌리거나(> <), 다른 명령을 끼워 넣는(` $( ) 표시. 허용 목록 명령이어도 승인
COMPOUND = re.compile(r"[;&|`<>\r\n]|\$\(")
# 환경 변수 값을 쏟는 명령 (웹 검색 API 키가 환경 변수에 있다). 경로로 쓰는 $env:USERPROFILE 같은 것은 괜찮다
ENV_READ = re.compile(
    r"(?i)(?<![\w$])env:"                                   # PowerShell 환경 변수 드라이브: dir env:, Get-Item env:X
    r"|^\s*\$env:\w+\s*$"                                    # $env:X 만 (값을 그대로 출력)
    r"|\b(?:echo|write-output|write-host|write)\b[^|;]*\$env:"   # 값을 출력
    r"|\$\{?\w*(?:key|token|secret|passw)\w*"                # $TAVILY_API_KEY, ${GH_TOKEN} (bash·zsh)
    r"|^\s*(?:printenv|env|set)(?:\s|$)|getenvironmentvariable|/proc/[\w/]*environ|os\.environ|getenv\b"
    r"|^\s*ps\s+(?!-)\S*e"                                   # ps e, ps axe (BSD 모양: 환경 변수까지 보임)
)
# 밖으로 글을 보내는 도구와 그 글이 든 인자
WEB_TOOLS: dict[str, str] = {"web.search": "query", "web.fetch": "url", "papers.search": "query"}   # 밖으로 나가는 글


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    # 승인 상자에 보여 줄 카드 [(모양, 글)]: 승인이 필요할 때 루프가 도구의 preview로 채운다 (remote.run)
    card: list[tuple[str, str]] = field(default_factory=list, compare=False, repr=False)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str              # 감사 로그용 규칙 이름
    reason: str            # 한 줄 이유 (한국어: 기록과 모델에게 돌려주는 관찰에 씀)
    grantable: bool = True  # "이 작업 동안" 승인을 받을 수 있는지
    key: str = ""          # 화면 언어로 바꿀 때 쓰는 키 (i18n "policy.*")
    params: tuple[tuple[str, Any], ...] = ()


def _d(v: Verdict, rule_name: str, key: str, /, *, grantable: bool = True, **params: Any) -> Decision:
    """이유 글은 표(i18n)의 한국어로 만들고, 키와 값도 함께 담는다."""
    items = tuple(params.items())
    probe = Decision(v, rule_name, "", grantable, key, items)
    return Decision(v, rule_name, localized(probe, lang="ko"), grantable, key, items)


def _why(loc: Location) -> dict[str, str]:
    return {"why_key": loc.why_key, "why_arg": loc.why_arg}


@dataclass
class PolicyContext:
    places: Places
    registered_tools: frozenset[str] = frozenset()
    source: str = "text"            # "text" | "voice"
    stage: str | None = None        # 실험 단계 이름 (예: "hypothesis", "design", "implement")
    grants: set[tuple[str, str]] = field(default_factory=set)  # (도구, 규칙) - "이 작업 동안" 승인
    private_names: tuple[str, ...] = ()       # 밖으로 보내면 묻는 이름 ([privacy] names, [user] name)
    # (이 컴퓨터의 도구가 돌려준 글, 연구원님이 쓴 글): 웹으로 나가는 글에 파일 내용이 들었는지 볼 때. 루프가 넣는다
    texts: Callable[[], tuple[str, str]] | None = None
    workspace: str = ""          # GLaDOS가 새 파일을 만들고 명령을 돌려도 되는 곳 (막혔을 때 모델에게 알려 줌)
    remote_user: str = ""        # 공유 서버의 내 사용자 이름 ([user] name). 다른 사람 폴더를 가릴 때
    remote_dirs: dict[str, str] = field(default_factory=dict)   # 서버 별칭 -> 내 작업 폴더 (servers.toml)


class Policy:
    def __init__(self, layers: list[tuple[str, dict[str, Any]]]):
        self.layers = layers

    # ------------------------------------------------------------------
    def check(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        hard = self._hard_rules(call, ctx)
        if hard is not None:
            return hard

        subject, location = self._subject(call, ctx)
        decision = self._layers(call.tool, subject, location, ctx)
        if call.tool == "shell.run":
            decision = self._shell(call, ctx, decision)
        if call.tool in WEB_TOOLS:
            decision = self._web(call, ctx, decision)
        if call.tool == "remote.exec" and decision.verdict != "deny":
            command = str(call.args.get("command", ""))
            if REMOTE_DESTRUCTIVE.search(command):
                decision = _d("ask", "코드:서버지우기", "policy.remote_destructive", grantable=False)
            elif REMOTE_BACKGROUND.search(command):
                decision = _d("ask", "코드:서버실행", "policy.remote_background", grantable=False)
            elif decision.verdict == "allow" and COMPOUND.search(command):
                decision = _d("ask", "코드:명령잇기", "policy.ask_compound")   # 허용 목록 명령이어도 잇거나 돌리면 승인
        # 서버 실행과 멈춤은 정책 파일이 allow여도 매번 승인 (실행 카드를 보고 연구원님이 정한다)
        if call.tool == "remote.run" and decision.verdict != "deny":
            decision = _d("ask", "코드:서버실행", "policy.remote_run", grantable=False)
        if call.tool == "remote.stop" and decision.verdict != "deny":
            decision = _d("ask", "코드:실행멈춤", "policy.remote_stop", grantable=False)
        if call.tool == "memory.note" and decision.verdict != "deny":
            if privacy.secret_in_note(str(call.args.get("text", ""))):
                return _d("deny", "코드:비밀기억", "policy.memory_secret", grantable=False)
            decision = _d("ask", "코드:기억", "policy.memory_note", grantable=False)

        # 셸 명령의 관리자 권한은 정책 파일과 상관없이 항상 승인 (작업 동안 승인 불가)
        if call.tool == "shell.run" and call.args.get("admin") and decision.verdict != "deny":
            return _d("ask", "코드:관리자권한", "policy.admin_shell", grantable=False)

        if decision.verdict == "ask" and decision.grantable and (call.tool, decision.rule) in ctx.grants:
            return Decision("allow", decision.rule, decision.reason + " (이 작업 동안 승인됨)",
                            key=decision.key, params=decision.params)
        return decision

    # ------------------------------------------------------------------
    def _hard_rules(self, call: ToolCall, ctx: PolicyContext) -> Decision | None:
        tool, args = call.tool, call.args

        if tool not in ctx.registered_tools:
            return _d("deny", "코드:미등록도구", "policy.unregistered", grantable=False, tool=tool)

        if ctx.stage in READONLY_STAGES and tool in WRITE_OR_EXEC:
            return _d("deny", "코드:읽기전용단계", "policy.readonly_stage", grantable=False)

        if ctx.source == "voice" and tool not in VOICE_TOOLS:
            return _d("deny", "코드:음성", "policy.voice", grantable=False)

        if tool in PATH_TOOLS and PATH_TOOLS[tool] == "write":
            for raw in _paths(call):
                loc = classify(raw, ctx.places)
                if loc.safety:
                    return _d("deny", "코드:안전장치", "policy.safety_path", grantable=False, **_why(loc))

        if tool == "shell.run":
            command = str(args.get("command", ""))
            if mentions_safety(command, ctx.places) or any(
                loc.safety for loc in command_locations(command, ctx.places, _shell_cwd(call, ctx))
            ):
                return _d("deny", "코드:안전장치", "policy.safety_command", grantable=False)
            if not args.get("admin") and ADMIN_WORDS.search(command):
                return _d("deny", "코드:관리자표시", "policy.admin_inline", grantable=False)

        if tool in ("remote.exec", "remote.run") and REMOTE_SUDO.search(str(args.get("command", ""))):
            return _d("deny", "코드:공유서버", "policy.remote_sudo", grantable=False)
        if tool in REMOTE_TOOLS:
            text = str(args.get("path" if tool == "remote.log" else "command", ""))
            if tool in ("remote.exec", "remote.run") and REMOTE_PROC.search(text):
                return _d("deny", "코드:공유서버", "policy.remote_proc", grantable=False)
            if tool == "remote.run":
                text += " " + str(args.get("cwd", ""))
            other = _others_path(text, ctx.remote_user)
            if other:
                return _d("deny", "코드:남의파일", "policy.remote_others", grantable=False, path=other)
            if tool == "remote.log" and text.startswith("/"):
                home = f"/home/{ctx.remote_user}" if ctx.remote_user else ""
                work = ctx.remote_dirs.get(str(args.get("server", "")), "").rstrip("/")
                if not any(r and (text == r or text.startswith(r + "/")) for r in (home, work)):
                    return _d("deny", "코드:남의파일", "policy.remote_outside", grantable=False, path=text)

        return None

    # ------------------------------------------------------------------
    def _shell(self, call: ToolCall, ctx: PolicyContext, decision: Decision) -> Decision:
        """셸 명령은 글 패턴만으로는 모자라서 더 본다.

        - 실행 폴더가 GLaDOS 안전장치면: 허용 목록의 단순한 명령만, 나머지는 차단 (승인으로도 못 풂)
        - 보호 위치(실행 폴더 또는 명령 속 경로)를 건드리면: 매번 승인 ("이 작업 동안" 안 됨)
        - 허용 목록 명령이어도 잇거나 돌리면(; && | > `): 승인
        """
        if decision.verdict == "deny":
            return decision
        command = str(call.args.get("command", ""))
        simple = not COMPOUND.search(command)
        start = _shell_cwd(call, ctx)
        cwd = command_cwd(command, ctx.places, start) or start   # 맨 앞에서 cd로 옮기면 그 폴더에서 도는 것
        cwd_loc = classify(cwd, ctx.places)
        if cwd_loc.safety:
            if decision.verdict == "allow" and simple:
                return decision
            return _d("deny", "코드:안전장치폴더", "policy.safety_cwd", grantable=False, **_why(cwd_loc))
        touched = next((loc for loc in (cwd_loc, *command_locations(command, ctx.places, start)) if loc.protected), None)
        if touched is not None:
            return _d("ask", "코드:보호위치명령", "policy.ask_protected", grantable=False, **_why(touched))
        if ENV_READ.search(command):
            return _d("ask", "코드:환경변수", "policy.ask_env", grantable=False)
        if decision.verdict == "allow" and not simple:
            return _d("ask", "코드:명령잇기", "policy.ask_compound")
        return decision

    def _web(self, call: ToolCall, ctx: PolicyContext, decision: Decision) -> Decision:
        """밖으로 나가는 글을 본다 (M3). 걸리면 매번 승인 ("이 작업 동안" 안 됨: 보낼 글이 매번 다르다).

        - web.fetch의 주소가 이 컴퓨터나 내부 네트워크 (공유기 관리 화면, 다른 프로그램의 로컬 API 등)
        - 보낼 글(검색어, 주소의 질의 부분)에 개인 정보나 이 대화에서 읽은 파일의 글
        """
        if decision.verdict == "deny":
            return decision
        text = str(call.args.get(WEB_TOOLS[call.tool], ""))
        if call.tool == "web.fetch":
            host = privacy.url_host(text)
            if host and privacy.is_local_host(host):
                return _d("ask", "코드:내부주소", "policy.ask_local_url", grantable=False, host=host)
        local, user = ctx.texts() if ctx.texts is not None else ("", "")
        scan = privacy.scan_url if call.tool == "web.fetch" else privacy.scan_text
        found = scan(text, names=ctx.private_names, home=ctx.places.home, local=local, user=user)
        if found:
            return _d("ask", "코드:개인정보", "policy.ask_private", grantable=False,
                      what_keys=",".join(f"privacy.{f.kind}" for f in found),
                      sample=" · ".join(f.sample for f in found))
        return decision

    def _subject(self, call: ToolCall, ctx: PolicyContext) -> tuple[str, Location | None]:
        """규칙 패턴과 비교할 글: 경로 도구는 경로, 명령 도구는 명령."""
        if call.tool in PATH_TOOLS:
            paths = _paths(call)
            if not paths:
                return "", None
            # 여러 경로면 가장 엄한 것을 기준으로
            locs = [classify(p, ctx.places) for p in paths]
            worst = next((loc for loc in locs if loc.protected), locs[0])
            return worst.path, worst
        for key in ("command", "query", "url", "server", "text"):
            if key in call.args:
                return str(call.args[key]).strip(), None
        return "", None

    def _layers(self, tool: str, subject: str, location: Location | None, ctx: PolicyContext) -> Decision:
        verdict: str | None = None
        rule = ""
        for layer_name, rules in self.layers:
            tool_rules = rules.get(tool)
            if not isinstance(tool_rules, dict):
                continue
            for pattern, value in tool_rules.items():
                if _matches(str(pattern), subject, location, ctx.places):
                    verdict = str(value).strip().lower()
                    rule = f"{layer_name}:{tool}:{pattern}"
        if verdict is None:
            return _d("deny", f"없음:{tool}", "policy.no_rule")
        if verdict not in VERDICTS:
            return _d("deny", rule, "policy.bad_value", verdict=verdict)
        if verdict == "ask":
            key, params = _ask_reason(tool, location)
            return _d("ask", rule, key, **params)
        return _d(verdict, rule, f"policy.{verdict}")  # type: ignore[arg-type]


def _others_path(text: str, me: str) -> str:
    """명령·경로 속 다른 사람 폴더 (없으면 ""). 내 사용자 이름을 모르면 /home/아무개는 모두 다른 사람으로 본다."""
    for m in OTHERS_PATH.finditer(text):
        user = m.group(1) or m.group(2)
        if user is None:                                   # /root
            if me != "root":
                return m.group(0)
        elif user != me:
            return m.group(0)
    return ""


def _shell_cwd(call: ToolCall, ctx: PolicyContext) -> str:
    """명령을 실행할 폴더: 인자의 cwd, 없으면 세션 작업 폴더, 없으면 홈 (그 OS 표기의 절대 경로)."""
    return normalize(str(call.args.get("cwd") or ctx.places.cwd or ctx.places.home), ctx.places)


def _paths(call: ToolCall) -> list[str]:
    args = call.args
    out: list[str] = []
    for key in ("path", "root"):
        if args.get(key):
            out.append(str(args[key]))
    for key in ("paths", "roots"):
        val = args.get(key)
        if isinstance(val, list):
            out.extend(str(v) for v in val if v)
    return out


def _matches(pattern: str, subject: str, location: Location | None, places: Places) -> bool:
    if pattern == "@protected":
        return bool(location and location.protected)
    if pattern == "*":
        return True
    if location is not None:
        # 경로 패턴은 같은 방식으로 풀어서 비교 (~, 대소문자, 구분자)
        pat = pattern
        if pat.startswith("~") or "/" in pat or "\\" in pat:
            pat = normalize(pat, places)
        return fnmatch.fnmatchcase(fold(places.os, subject), fold(places.os, pat))
    if places.os == "windows":
        return fnmatch.fnmatchcase(subject.casefold(), pattern.casefold())
    return fnmatch.fnmatchcase(subject, pattern)


def _ask_reason(tool: str, location: Location | None) -> tuple[str, dict[str, str]]:
    if location is not None and location.protected:
        return "policy.ask_protected", _why(location)
    return {
        "fs.edit": "policy.ask_edit",
        "fs.trash": "policy.ask_trash",
        "shell.run": "policy.ask_shell",
    }.get(tool, "policy.ask_default"), {}
