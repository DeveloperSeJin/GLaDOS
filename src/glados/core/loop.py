"""에이전트 루프.

계획 -> 도구 호출 제안(JSON) -> 스키마 검증 -> 정책 판정 -> 실행 -> 관찰 -> 확인 -> 끝났나?

한도 (glados.toml [loop])
- 한 작업 최대 12번, 넘으면 멈추고 보고
- 형식 오류는 2번까지 다시 요청 (llm.py), 그다음 멈춤
- 같은 호출이 3번 이어지면 멈춤
- 같은 도구 오류가 2번이면 멈추고 보고
차단되거나 거절되면 그 사실을 관찰로 돌려주고 계획으로 돌아간다.
이 함수 하나를 터미널 화면, 나중의 glados serve가 똑같이 부른다.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from glados.config import LoopLimits
from glados.core import events as ev
from glados.core.events import Event
from glados.core.llm import FormatError
from glados.core.schema import Answer, Step, step_schema
from glados.hands import ToolError, ToolRegistry
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

OBS_LIMIT = 4000   # 이보다 긴 도구 결과는 앞뒤만 남기고 전체는 파일로
OBS_HEAD = 1500
OBS_TAIL = 1500

ApproveFn = Callable[[ToolCall, Decision], str]       # "once" | "task" | "deny"
AuditFn = Callable[[ToolCall, Decision, str], None]   # (호출, 판정, 결정한 사람)
EmitFn = Callable[[Event], None]


class ChatModel(Protocol):
    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        validate: Callable[[dict[str, Any]], Any],
        retries: int = 2,
        temperature: float = 0.2,
    ) -> tuple[Any, str]: ...


@dataclass
class TaskResult:
    status: str                          # "done" | "stopped"
    answer: Answer | None = None
    sources: list[str] = field(default_factory=list)
    steps: int = 0
    reason: str = ""                     # max_steps | same_call | tool_errors | format
    message: str = ""                    # 연구원님께 보여 줄 한 줄


def run_task(
    request: str,
    *,
    llm: ChatModel,
    policy: Policy,
    ctx: PolicyContext,
    tools: ToolRegistry,
    limits: LoopLimits,
    system_prompt: str,
    history: Iterable[dict[str, str]] = (),
    emit: EmitFn | None = None,
    approve: ApproveFn | None = None,
    audit: AuditFn | None = None,
    overflow_dir: Path | None = None,
    task_id: str = "",
) -> TaskResult:
    emit = emit or (lambda e: None)
    approve = approve or (lambda call, d: "deny")
    audit = audit or (lambda call, d, who: None)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt.rstrip() + "\n\n" + tools.describe()}
    ]
    messages += list(history)
    messages.append({"role": "user", "content": request})
    schema = step_schema(tools.names())
    emit(Event(ev.START, {"request": request}))

    last_key = ""
    same = 0
    errors: Counter[str] = Counter()

    for step_no in range(1, limits.max_steps + 1):
        try:
            step, raw = llm.chat_json(messages, schema, Step.model_validate, retries=limits.format_retries)
        except FormatError as e:
            return _stop(emit, "format", step_no, "모델 출력 형식이 계속 맞지 않아 멈췄습니다.", str(e))
        messages.append({"role": "assistant", "content": raw})
        if step.thought:
            emit(Event(ev.THOUGHT, {"step": step_no, "thought": step.thought}))

        if step.done:
            assert step.answer is not None
            emit(Event(ev.ANSWER, {"fact": step.answer.fact, "wit": step.answer.wit, "sources": step.sources}))
            return TaskResult("done", step.answer, list(step.sources), step_no)

        assert step.action is not None
        call = ToolCall(step.action.tool, dict(step.action.args))
        emit(Event(ev.PROPOSAL, {"step": step_no, "tool": call.tool, "args": call.args}))

        key = json.dumps([call.tool, call.args], sort_keys=True, ensure_ascii=False, default=str)
        same = same + 1 if key == last_key else 1
        last_key = key
        if same >= limits.same_call_limit:
            return _stop(emit, "same_call", step_no, f"같은 호출({call.tool})이 {same}번 이어져 멈췄습니다.")

        decision = policy.check(call, ctx)
        allowed, who, note = _decide(call, decision, ctx, approve)
        audit(call, decision, who)
        emit(Event(ev.DECISION, {
            "tool": call.tool, "verdict": decision.verdict, "rule": decision.rule,
            "reason": decision.reason, "decided_by": who,
        }))

        if not allowed:
            observation = note
        else:
            timeout = limits.remote_timeout_s if call.tool.startswith("remote.") else limits.local_timeout_s
            try:
                observation = _clip(tools.run(call, timeout_s=timeout), overflow_dir, task_id, step_no)
            except ToolError as e:
                errors[call.tool] += 1
                observation = f"오류: {e}"
                emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": observation}))
                if errors[call.tool] >= limits.tool_error_limit:
                    return _stop(emit, "tool_errors", step_no, f"{call.tool} 오류가 {errors[call.tool]}번 나서 멈췄습니다.", str(e))
                messages.append({"role": "user", "content": f"[관찰: {call.tool}] {observation}"})
                continue

        emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": observation[:500]}))
        messages.append({"role": "user", "content": f"[관찰: {call.tool}] {observation}"})

    return _stop(emit, "max_steps", limits.max_steps, f"한 작업 최대 {limits.max_steps}번을 넘어 멈췄습니다.")


def _decide(call: ToolCall, d: Decision, ctx: PolicyContext, approve: ApproveFn) -> tuple[bool, str, str]:
    """(실행할지, 결정한 사람, 막혔을 때 모델에게 돌려줄 관찰)"""
    if d.verdict == "allow":
        return True, "정책", ""
    if d.verdict == "deny":
        return False, "정책", f"차단: {d.reason}. 다른 방법을 찾거나 할 수 없다고 보고하세요."
    if d.verdict == "wait":
        return False, "정책", f"대기: {d.reason}"
    choice = approve(call, d)
    if choice == "task" and d.grantable:
        ctx.grants.add((call.tool, d.rule))
        return True, "연구원님:이 작업 동안", ""
    if choice in ("once", "task"):
        return True, "연구원님:이번만", ""
    return False, "연구원님:거절", "연구원님이 거절했습니다. 다른 방법을 찾거나 그대로 보고하세요."


def _clip(text: str, overflow_dir: Path | None, task_id: str, step_no: int) -> str:
    if len(text) <= OBS_LIMIT:
        return text
    where = ""
    if overflow_dir is not None:
        overflow_dir.mkdir(parents=True, exist_ok=True)
        path = overflow_dir / f"{task_id or 'task'}-{step_no}.txt"
        path.write_text(text, encoding="utf-8")
        where = f", 전체: {path}"
    return f"{text[:OBS_HEAD]}\n...(중략, 전체 {len(text)}자{where})...\n{text[-OBS_TAIL:]}"


def _stop(emit: EmitFn, reason: str, steps: int, message: str, detail: str = "") -> TaskResult:
    emit(Event(ev.STOP, {"reason": reason, "message": message, "detail": detail}))
    return TaskResult("stopped", steps=steps, reason=reason, message=message)
