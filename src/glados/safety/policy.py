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
   - 공유 서버 sudo -> 차단
2. policy.yaml 층 (기본 -> 연구원님 -> 서버 -> 저장소). 마지막에 맞는 규칙이 이긴다. 아무것도 안 맞으면 차단
3. "이 작업 동안" 승인을 받은 규칙이면 ask -> allow
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from glados.i18n import localized
from glados.safety.locations import (
    Location, Places, classify, command_locations, fold, mentions_safety, normalize,
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
REMOTE_SUDO = re.compile(r"(^|[\s;&|(])sudo(\s|$)")
# 명령을 잇거나(; && || |), 출력을 돌리거나(> <), 다른 명령을 끼워 넣는(` $( ) 표시. 허용 목록 명령이어도 승인
COMPOUND = re.compile(r"[;&|`<>\r\n]|\$\(")


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


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

        # 셸 명령의 관리자 권한은 정책 파일과 상관없이 항상 승인 (작업 동안 승인 불가)
        if call.tool == "shell.run" and call.args.get("admin") and decision.verdict != "deny":
            return _d("ask", "코드:관리자권한", "policy.admin_shell", grantable=False)

        if decision.verdict == "ask" and (call.tool, decision.rule) in ctx.grants:
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

        if tool == "remote.exec" and REMOTE_SUDO.search(str(args.get("command", ""))):
            return _d("deny", "코드:공유서버", "policy.remote_sudo", grantable=False)

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
        cwd = _shell_cwd(call, ctx)
        cwd_loc = classify(cwd, ctx.places)
        if cwd_loc.safety:
            if decision.verdict == "allow" and simple:
                return decision
            return _d("deny", "코드:안전장치폴더", "policy.safety_cwd", grantable=False, **_why(cwd_loc))
        touched = next((loc for loc in (cwd_loc, *command_locations(command, ctx.places, cwd)) if loc.protected), None)
        if touched is not None:
            return _d("ask", "코드:보호위치명령", "policy.ask_protected", grantable=False, **_why(touched))
        if decision.verdict == "allow" and not simple:
            return _d("ask", "코드:명령잇기", "policy.ask_compound")
        return decision

    def _subject(self, call: ToolCall, ctx: PolicyContext) -> tuple[str, Location | None]:
        """규칙 패턴과 비교할 글: 경로 도구는 경로, 명령 도구는 명령."""
        if call.tool in PATH_TOOLS:
            paths = _paths(call)
            if not paths:
                return "", None
            # 여러 경로면 가장 엄한 것을 기준으로
            locs = [classify(p, ctx.places) for p in paths]
            worst = next((l for l in locs if l.protected), locs[0])
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
