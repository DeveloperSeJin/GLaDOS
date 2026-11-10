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

from glados.safety.locations import Location, Places, classify, fold, mentions_safety, normalize

Verdict = Literal["allow", "ask", "deny", "wait"]
VERDICTS = ("allow", "ask", "deny", "wait")

# 경로를 받는 도구와 그 성격
PATH_TOOLS: dict[str, str] = {
    "fs.search": "read",
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


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str              # 감사 로그용 규칙 이름
    reason: str            # 연구원님께 보여 줄 한 줄
    grantable: bool = True  # "이 작업 동안" 승인을 받을 수 있는지


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

        # 셸 명령의 관리자 권한은 정책 파일과 상관없이 항상 승인 (작업 동안 승인 불가)
        if call.tool == "shell.run" and call.args.get("admin") and decision.verdict != "deny":
            return Decision("ask", "코드:관리자권한", "관리자 권한으로 실행합니다 (OS 확인 창이 한 번 더 뜹니다)", grantable=False)

        if decision.verdict == "ask" and (call.tool, decision.rule) in ctx.grants:
            return Decision("allow", decision.rule, decision.reason + " (이 작업 동안 승인됨)")
        return decision

    # ------------------------------------------------------------------
    def _hard_rules(self, call: ToolCall, ctx: PolicyContext) -> Decision | None:
        tool, args = call.tool, call.args

        if tool not in ctx.registered_tools:
            return Decision("deny", "코드:미등록도구", f"등록되지 않은 도구입니다 ({tool})", grantable=False)

        if ctx.stage in READONLY_STAGES and tool in WRITE_OR_EXEC:
            return Decision("deny", "코드:읽기전용단계", "가설·설계 단계에서는 쓰기·실행을 하지 않습니다", grantable=False)

        if ctx.source == "voice" and tool not in VOICE_TOOLS:
            return Decision("deny", "코드:음성", "음성 요청으로는 정해진 명령만 실행합니다", grantable=False)

        if tool in PATH_TOOLS and PATH_TOOLS[tool] == "write":
            for raw in _paths(call):
                loc = classify(raw, ctx.places)
                if loc.safety:
                    return Decision("deny", "코드:안전장치", f"GLaDOS 안전장치는 고칠 수 없습니다 ({loc.why})", grantable=False)

        if tool == "shell.run":
            command = str(args.get("command", ""))
            if mentions_safety(command, ctx.places):
                return Decision("deny", "코드:안전장치", "GLaDOS 안전장치 위치를 건드리는 명령입니다", grantable=False)
            if not args.get("admin") and ADMIN_WORDS.search(command):
                return Decision(
                    "deny", "코드:관리자표시",
                    "관리자 권한은 명령 안에서 올리지 않고 admin 표시로 요청해야 합니다", grantable=False,
                )

        if tool == "remote.exec" and REMOTE_SUDO.search(str(args.get("command", ""))):
            return Decision("deny", "코드:공유서버", "공유 서버에서 sudo는 쓰지 않습니다", grantable=False)

        return None

    # ------------------------------------------------------------------
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
                return str(call.args[key]), None
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
            return Decision("deny", f"없음:{tool}", "맞는 정책 규칙이 없어 차단합니다")
        if verdict not in VERDICTS:
            return Decision("deny", rule, f"정책 값이 잘못되었습니다 ({verdict})")
        reason = {
            "allow": "허용",
            "ask": _ask_reason(tool, location),
            "deny": "정책에 따라 차단합니다",
            "wait": "조건이 맞을 때까지 기다립니다",
        }[verdict]
        return Decision(verdict, rule, reason)  # type: ignore[arg-type]


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


def _ask_reason(tool: str, location: Location | None) -> str:
    if location is not None and location.protected:
        return f"보호 위치입니다: {location.why}"
    return {
        "fs.edit": "원본 파일을 고칩니다 (고치기 전에 체크포인트를 남깁니다)",
        "fs.trash": "파일을 휴지통으로 옮깁니다",
        "shell.run": "허용 목록 밖의 명령입니다",
    }.get(tool, "승인이 필요한 작업입니다")
