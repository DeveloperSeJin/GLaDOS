"""에이전트 루프.

계획 -> 도구 호출 제안(JSON) -> 스키마 검증 -> 정책 판정 -> 실행 -> 관찰 -> 확인 -> 끝났나?
첫 걸음에는 모델이 계획(plan)을 먼저 쓴다: 어떻게 할지는 모델이 정한다 (도구가 있을 때. 스키마로 강제)

한도 (glados.toml [loop])
- 한 작업 최대 12번, 넘으면 멈추고 보고
- 형식 오류는 2번까지 다시 요청 (llm.py), 그다음 멈춤
- 같은 호출이 3번 이어지면 멈춤
- 같은 도구 오류가 2번이면 멈추고 보고
차단되거나 거절되면 그 사실을 관찰로 돌려주고 계획으로 돌아간다.

영어로 통일 (M2.5)
- 루프 안의 글(도구 결과, 안내, 멈춤 보고)은 모두 영어다. 모델은 영어로 생각하고 영어로 답한다
- 답을 받으면 연구원님의 언어(reply_lang)로 번역해 화면에 낸다 (translator). 기억에는 영어 답이 남는다
- 모델이 영어로 쓰지 않았으면 한 번 되돌린다 (그래야 안전망이 영어 한 벌로 비교한다)

대화 기억 (core/context.py)
- 지난 작업(걸음, 도구 결과, 답)을 그대로 앞에 넣는다. 맥락 길이를 넘으면 오래된 것부터 줄인다
- 모델을 부르기 전마다 길이를 확인한다 (Ollama가 앞 대화를 몰래 자르지 않게)
- 이번 작업의 기록(transcript)을 돌려준다. 다음 작업이 이것을 기억한다

작은 모델을 위한 안전망 (원칙 4: 모델은 틀린다고 가정한다)
- 근거 확인: 파일을 다루는 이야기인데 이번 작업에서 도구를 한 번도 부르지 않고 답하면서, 출처가 앞에서
  확인한 자료에 없으면 한 번 되돌린다 (앞 대화에서 읽은 파일을 출처로 대면 그대로 받는다.
  도구를 불렀다가 차단·거절된 뒤의 답은 그 사실을 말하는 것이라 되돌리지 않는다)
- 영어 확인, 답 안전망(guards.py): 각각 한 번만 되돌린다
- 같은 결과 안내: 같은 도구가 같은 결과를 또 내면 "지금까지 확인한 것으로 답하라"고 덧붙인다
- 멈출 때 보고: 같은 호출·최대 횟수·도구 오류로 멈추면, 도구 없이 지금까지 확인한 것만으로 한 번 답하게 한다

연구원님이 멈춤 (TUI의 Esc·Ctrl+C)
- cancelled()가 참이면 다음 걸음 전에 멈춘다. 모델 호출 중이면 llm.py가 Cancelled를 내고, 여기서 받아 멈춘다
- 멈춘 작업도 기억에 남는다 ("[stopped] Stopped by the Researcher.")

이 함수 하나를 줄 화면, TUI, 나중의 glados serve가 똑같이 부른다.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from glados.config import LoopLimits
from glados.core import events as ev
from glados.core.context import CHECK_MARK, STOP_MARK, TASK_RESERVE, Conversation, fit, grounded, observation, size
from glados.core.events import Event
from glados.core.guards import Guard
from glados.core.llm import Cancelled, FormatError
from glados.core.schema import Answer, Step, step_schema
from glados.core.translate import Translator, in_language
from glados.hands import ToolError, ToolRegistry
from glados.i18n import localized, t
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

OBS_LIMIT = 4000   # 이보다 긴 도구 결과는 앞뒤만 남기고 전체는 파일로
OBS_HEAD = 1500
OBS_TAIL = 1500
TRAIL_HEAD = 600   # 작업 메모에 남길 결과 앞부분
MAX_PLAN = 8       # 화면에 보여 줄 계획 단계 수

NUDGE_EVIDENCE = (
    f"{CHECK_MARK} You used no tool in this task, and your sources are not in any earlier tool result. "
    "If the answer is about files (whether they exist, how many, what they say), check with a tool first. "
    "If you already found or read the file earlier in this conversation, put its path in sources. "
    "If the question is not about files, give the same answer again."
)
NUDGE_ENGLISH = (
    f"{CHECK_MARK} Write answer.fact and answer.wit in English. "
    "The answer is translated for the Researcher automatically."
)
NUDGE_ACT = (
    f"{CHECK_MARK} Your plan is noted. Now take its first step: put a tool call in action and set done to false, "
    "or, if no tool is needed, set done to true and answer."
)
HINT_REPEAT = "\n[note] Same result as before. Unless you change the conditions a lot, answer with what you have."
FINAL_REPORT = (
    "{mark} {why} Do not call more tools. Answer only with what you have confirmed so far, "
    "and say plainly what you could not confirm."
)

ApproveFn = Callable[[ToolCall, Decision], str]       # "once" | "task" | "deny"
AuditFn = Callable[[ToolCall, Decision, str], None]   # (호출, 판정, 결정한 사람)
EmitFn = Callable[[Event], None]
Message = dict[str, str]


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
    answer: Answer | None = None         # 화면에 낸 답 (연구원님의 언어). 멈춘 경우에도 마지막 보고가 있으면 들어 있음
    sources: list[str] = field(default_factory=list)
    steps: int = 0
    reason: str = ""                     # max_steps | same_call | tool_errors | format
    message: str = ""                    # 멈춘 이유 한 줄 (영어)
    trail: list[dict[str, Any]] = field(default_factory=list)       # 실행한 도구와 결과 앞부분 (작업 메모)
    transcript: list[Message] = field(default_factory=list)         # 이번 작업의 걸음과 관찰 (다음 작업이 기억)
    internal: Answer | None = None       # 모델이 쓴 영어 답 (기억에 남는 것). 번역하지 않았으면 answer와 같다


def run_task(
    request: str,
    *,
    llm: ChatModel,
    policy: Policy,
    ctx: PolicyContext,
    tools: ToolRegistry,
    limits: LoopLimits,
    system_prompt: str,
    memory: Conversation | None = None,
    history: Iterable[Message] = (),
    context_chars: int | None = None,
    emit: EmitFn | None = None,
    approve: ApproveFn | None = None,
    audit: AuditFn | None = None,
    overflow_dir: Path | None = None,
    task_id: str = "",
    evidence_check: Callable[[str], bool] | None = None,
    answer_guards: Iterable[Guard] = (),
    final_note: str = "",
    translator: Translator | None = None,
    reply_lang: str = "en",
    cancelled: Callable[[], bool] | None = None,
) -> TaskResult:
    """memory가 있으면 지난 대화를 길이에 맞게 넣는다 (없으면 history를 그대로).
    context_chars: 보낼 수 있는 전체 글자 수 (context.limit_chars). 없으면 줄이지 않는다.
    evidence_check: 글(요청 + 모델의 생각과 답)이 파일을 다루는지. 그렇다면 근거를 확인한다.
    answer_guards: 답을 받기 전 안전망 (guards.py). 각각 한 작업에서 한 번만 되돌린다.
    final_note: 시스템 메시지 맨 끝(도구 목록 뒤)에 붙일 지시 (언어 안내).
    translator, reply_lang: 모델의 영어 답을 연구원님의 언어로 옮겨 화면에 낸다 ("en"이면 옮기지 않음).
    cancelled: 연구원님이 멈췄는지 (TUI). 참이면 다음 걸음 전에 멈춘다."""
    emit = emit or (lambda e: None)
    cancelled = cancelled or (lambda: False)
    approve = approve or (lambda call, d: "deny")
    audit = audit or (lambda call, d, who: None)

    parts = [system_prompt.rstrip(), tools.describe()] + ([final_note] if final_note else [])
    system: Message = {"role": "system", "content": "\n\n".join(parts)}
    task: list[Message] = [{"role": "user", "content": request}]   # 이번 작업 (보낼 때 쓰는 것, 줄일 수 있음)
    transcript: list[Message] = []                                  # 이번 작업 기록 (줄이지 않음, 되돌린 답은 뺌)
    rerender = memory.render if memory is not None else None
    if memory is not None:
        budget = None if context_chars is None else max(0, context_chars - size([system]) - size(task) - TASK_RESERVE)
        hist = memory.render(budget)
    else:
        hist = list(history)
    schema = step_schema(tools.specs())
    first = step_schema(tools.specs(), plan=True) if tools.names() else schema   # 첫 걸음: 어떻게 할지 먼저
    emit(Event(ev.START, {"request": request}))

    def outgoing() -> list[Message]:
        nonlocal hist, task
        if context_chars is not None:
            hist, task = fit(system, hist, task, context_chars, rerender)
        return [system, *hist, *task]

    def record(m: Message) -> None:
        task.append(m)
        transcript.append(m)

    last_key = ""
    same = 0
    errors: Counter[str] = Counter()
    trail: list[dict[str, Any]] = []
    last_obs: dict[str, str] = {}
    nudged = False
    english_nudged = False
    attempted = False          # 이번 작업에서 도구를 부르려 했는지 (차단·거절·오류도 포함)
    guards = list(answer_guards)
    fired: set[int] = set()    # 이미 되돌린 안전망
    planned_only = 0           # 계획만 쓰고 행동하지 않은 답 (한 번은 되돌려 준다)

    def send_back(step_no: int, key: str, nudge: str) -> None:
        """답을 받지 않고 한 번 되돌린다. 그 답은 화면에도 기억에도 남기지 않는다."""
        transcript.pop()
        emit(Event(ev.GUARD, {"step": step_no, "key": key, "message": t(key, "en")}))
        task.append({"role": "user", "content": nudge})

    def to_english(said: Answer) -> Answer:
        """영어가 아닌 답을 영어로 (안전망 비교와 기억용). 못 옮기면 그대로."""
        if translator is None or in_language(f"{said.fact} {said.wit}", "en"):
            return said
        done = translator.translate(said.fact, said.wit, "en")
        return Answer(fact=done[0], wit=done[1]) if done else said

    def deliver(said: Answer, sources: list[str], english: Answer | None = None) -> Answer:
        """모델의 답을 연구원님의 언어로 옮겨 화면에 낸다. 화면에 낸 답을 돌려준다."""
        shown, translated, failed = said, False, False
        text = f"{said.fact} {said.wit}"
        if reply_lang == "en" and english is not None and english is not said:
            shown, translated = english, True                       # 영어로 물었는데 다른 언어로 답한 경우
        elif translator is not None and not in_language(text, reply_lang):
            done = translator.translate(said.fact, said.wit, reply_lang)
            if done is None:
                failed = True
            else:
                shown, translated = Answer(fact=done[0], wit=done[1]), True
        emit(Event(ev.ANSWER, {
            "fact": shown.fact, "wit": shown.wit, "sources": sources,
            "lang": reply_lang, "translated": translated, "untranslated": failed,
            "original": {"fact": said.fact, "wit": said.wit},
        }))
        return shown

    def stop(reason: str, steps: int, detail: str = "", report: bool = False, **params: Any) -> TaskResult:
        """멈춘 이유는 영어로 (기억·기록·모델용). 화면은 reason과 params로 그 언어."""
        message = t(f"stop.{reason}", "en", **params)
        said, shown, sources = None, None, []
        final = _final_report(llm, outgoing(), message) if report else None
        transcript.append({"role": "user", "content": f"{STOP_MARK} {message}"})   # 기억에는 지시 없이 사실만
        if final is not None:
            step, raw = final
            sources = list(step.sources)
            said = to_english(step.answer)
            transcript.append({"role": "assistant", "content": raw})
            shown = deliver(step.answer, sources, said)
        emit(Event(ev.STOP, {"reason": reason, "message": message, "detail": detail,
                             "answered": said is not None, "params": params}))
        return TaskResult("stopped", shown, sources, steps, reason, message, trail, transcript, internal=said)

    current = 0                # 지금 걸음 (멈춤 보고용)
    try:
        for step_no in range(1, limits.max_steps + 1):
            current = step_no
            if cancelled():
                return stop("user", step_no - 1)
            try:
                step, raw = llm.chat_json(outgoing(), first if step_no == 1 else schema, Step.model_validate,
                                          retries=limits.format_retries)
            except FormatError as e:
                return stop("format", step_no, str(e))
            record({"role": "assistant", "content": raw})
            plan = [p.strip() for p in step.plan if p.strip()][:MAX_PLAN]
            if plan:
                emit(Event(ev.PLAN, {"step": step_no, "plan": plan}))
            if step.thought:
                emit(Event(ev.THOUGHT, {"step": step_no, "thought": step.thought}))

            if not step.done and step.action is None:              # 계획만 쓰고 행동은 안 함
                planned_only += 1
                if planned_only > 1:
                    return stop("format", step_no, "The model wrote a plan but did not take a step, twice.")
                transcript.pop()                                     # 기억에는 남기지 않는다 (계획은 화면에 이미 나옴)
                emit(Event(ev.GUARD, {"step": step_no, "key": "guard.act", "message": t("guard.act", "en")}))
                task.append({"role": "user", "content": NUDGE_ACT})
                continue

            if step.done:
                assert step.answer is not None
                said = f"{step.answer.fact} {step.answer.wit}"
                needs_check = (
                    not attempted and not nudged and tools.names()
                    and evidence_check is not None
                    and evidence_check(f"{request}\n{step.thought}\n{said}")
                    and not grounded(list(step.sources), [*hist, *task])
                )
                if needs_check:
                    nudged = True
                    send_back(step_no, "guard.evidence", NUDGE_EVIDENCE)
                    continue
                if not english_nudged and not in_language(said, "en"):
                    english_nudged = True
                    send_back(step_no, "guard.english", NUDGE_ENGLISH)
                    continue
                english = to_english(step.answer)                      # 그래도 영어가 아니면 옮겨서 비교 (영어로 통일)
                found = None
                for i, guard in enumerate(guards):
                    if i not in fired:
                        found = guard(f"{english.fact} {english.wit}", bool(tools.names()))
                        if found is not None:
                            fired.add(i)
                            break
                if found is not None:
                    send_back(step_no, *found)
                    continue
                shown = deliver(step.answer, list(step.sources), english)
                return TaskResult("done", shown, list(step.sources), step_no, trail=trail, transcript=transcript,
                                  internal=english)

            assert step.action is not None
            attempted = True
            call = ToolCall(step.action.tool, dict(step.action.args))
            emit(Event(ev.PROPOSAL, {"step": step_no, "tool": call.tool, "args": call.args}))

            key = json.dumps([call.tool, call.args], sort_keys=True, ensure_ascii=False, default=str)
            same = same + 1 if key == last_key else 1
            last_key = key
            if same >= limits.same_call_limit:
                return stop("same_call", step_no, report=True, tool=call.tool, count=same)

            decision = policy.check(call, ctx)
            allowed, who, note = _decide(call, decision, ctx, approve)
            audit(call, decision, who)
            emit(Event(ev.DECISION, {
                "tool": call.tool, "verdict": decision.verdict, "rule": decision.rule,
                "reason": decision.reason, "decided_by": who,
                "key": decision.key, "params": dict(decision.params),
            }))

            if not allowed:
                result = note
            else:
                timeout = limits.remote_timeout_s if call.tool.startswith("remote.") else limits.local_timeout_s
                tool = tools.get(call.tool)
                if tool is not None and tool.timeout_s > timeout:
                    timeout = tool.timeout_s                         # 오래 걸리는 도구 (fs.inspect: 파일마다 모델에게 물음)
                try:
                    result = _clip(tools.run(call, timeout_s=timeout, cancelled=cancelled), overflow_dir, task_id, step_no)
                except ToolError as e:
                    errors[call.tool] += 1
                    result = f"Error: {e}"
                    emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": result, "key": e.key, "params": e.params}))
                    record(observation(call.tool, result))
                    if errors[call.tool] >= limits.tool_error_limit:
                        return stop("tool_errors", step_no, localized(e), report=True, tool=call.tool, count=errors[call.tool])
                    continue
                trail.append({"tool": call.tool, "args": call.args, "head": result[:TRAIL_HEAD]})

            shown_text = result
            if allowed and last_obs.get(call.tool) == result:
                shown_text += HINT_REPEAT
            if allowed:
                last_obs[call.tool] = result
            emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": shown_text[:500]}))
            record(observation(call.tool, shown_text))

        return stop("max_steps", limits.max_steps, report=True, limit=limits.max_steps)
    except Cancelled:          # 모델 호출·도구·번역 중에 연구원님이 멈춤
        return stop("user", current)


def _final_report(llm: ChatModel, messages: list[Message], why: str) -> tuple[Step, str] | None:
    """멈출 때 도구 없이 한 번 답하게 한다. (걸음, 원문). 실패하면 None (멈춤 메시지만 남김)."""
    msgs = messages + [{"role": "user", "content": FINAL_REPORT.format(mark=STOP_MARK, why=why)}]
    try:
        step, raw = llm.chat_json(msgs, step_schema([]), Step.model_validate, retries=1)
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    return (step, raw) if step.done and step.answer is not None else None


def _decide(call: ToolCall, d: Decision, ctx: PolicyContext, approve: ApproveFn) -> tuple[bool, str, str]:
    """(실행할지, 결정한 사람, 막혔을 때 모델에게 돌려줄 관찰). 결정한 사람은 기록용 식별자(한국어)."""
    reason = localized(d, fallback=d.reason, lang="en")
    if d.verdict == "allow":
        return True, "정책", ""
    if d.verdict == "deny":
        return False, "정책", f"Blocked: {reason}. Find another way, or report that it cannot be done."
    if d.verdict == "wait":
        return False, "정책", f"Waiting: {reason}"
    choice = approve(call, d)
    if choice == "task" and d.grantable:
        ctx.grants.add((call.tool, d.rule))
        return True, "연구원님:이 작업 동안", ""
    if choice in ("once", "task"):
        return True, "연구원님:이번만", ""
    return False, "연구원님:거절", "The Researcher denied this. Find another way, or report it as it is."


def clip_output(text: str, overflow_dir: Path | None, task_id: str, step_no: int = 0) -> str:
    """긴 도구 결과는 앞뒤만 남기고 전체는 파일로 (화면의 !명령도 같은 규칙)."""
    return _clip(text, overflow_dir, task_id, step_no)


def _clip(text: str, overflow_dir: Path | None, task_id: str, step_no: int) -> str:
    if len(text) <= OBS_LIMIT:
        return text
    where = ""
    if overflow_dir is not None:
        overflow_dir.mkdir(parents=True, exist_ok=True)
        path = overflow_dir / f"{task_id or 'task'}-{step_no}.txt"
        path.write_text(text, encoding="utf-8")
        where = f"; full text: {path}"
    return f"{text[:OBS_HEAD]}\n...(clipped: {len(text)} chars in total{where})...\n{text[-OBS_TAIL:]}"
