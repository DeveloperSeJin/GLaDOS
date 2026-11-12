"""에이전트 루프.

계획 -> 도구 호출 제안(JSON) -> 스키마 검증 -> 정책 판정 -> 실행 -> 관찰 -> 확인 -> 끝났나?

계획 먼저 (M7.7, 연구원님 "움직이기 전에 어떻게 찾아야 하는지 계획을 먼저 세우는 방식으로")
- 도구가 있으면 행동하기 전에 계획만 받는 호출을 한 번 한다 (걸음 수에 세지 않는다). 시스템 메시지 끝에 이 컴퓨터의
  지금 사실(찾는 범위의 드라이브, 색인 상태, 웹 검색 여부: situation)을 붙여, 어디를 어떻게 찾을지 모델이 정한다
- 도구가 필요 없으면(인사, 아는 것) 그 답을 바로 낸다. 필요하면 목표·계획·확인 방법을 화면에 보여 주고 바로 진행한다
  (연구원님 결정: 승인을 기다리지 않음). 화면에는 연구원님의 언어로 옮겨 보인다
- 되묻기 (M7.8, 연구원님 "의도파악을 너무 못하는데": "섹스" 한 단어에 연구 과제를 지어내 논문을 12걸음 찾음):
  무엇을 원하는지 알 수 없으면 짐작하지 않고 짧은 질문 하나(뜻 두세 가지)로 작업을 끝낸다. 도구도, 밖으로 나가는
  검색도 없다. 질문은 안전망을 거치지 않는다 (사실을 말하는 답이 아니라서). 기억에는 남겨 다음 말과 이어지게 한다
- 목표는 연구원님이 말한 범위 그대로 (M7.8): 계획 뒤 안내에 목표를 다시 적고, 지어낸 좁은 질문으로 바꿔 답하지 말라고 한다
- 계획은 이번 작업에만 넣는다 (다음 작업의 기억에는 걸음들만 남는다)
- 계획 답이 형식을 못 맞추면 예전처럼 첫 걸음에 계획을 받는다 (M2.7)

한도 (glados.toml [loop])
- 한 작업 최대 12번, 넘으면 멈추고 보고
- 형식 오류는 2번까지 다시 요청 (llm.py), 그다음 멈춤
- 같은 호출이 3번 이어지면 멈춤
- 같은 도구 오류가 2번이면 멈추고 보고
- 정책에 3번 막히면 멈추고 보고 (막힌 일을 다른 방법으로 계속 두드리지 않게)
차단되거나 거절되면 그 사실을 관찰로 돌려주고 계획으로 돌아간다. GLaDOS 안전장치에 막히면 어디서 하면 되는지도 알려 준다.

언어 (M2.5 -> M8 -> M8.3)
- 모델은 전부 영어로 쓴다: 생각, 도구 인자, 대화 차례의 say, 답 (M8.3 연구원님 "그냥 전부 영어로 하라니까 한글은 번역투여도
  돼". M8에서 한국어로 바로 쓰게 했더니 생각·계획까지 한국어로 새고 말투가 흔들렸다)
- 화면에 낼 때만 연구원님의 언어로 옮긴다 (translator). 위트(wit)는 영어 그대로, 맞을 때만
- 답이 영어가 아니면 한 번 되돌린다. 그래도 아니면 그대로 받는다 (안전망은 영어 답에만)

물으면 기다리기 (M8.2, 연구원님 "이상한 말을 하네": 첫 말로 "목적과 가설을 정해 주시겠습니까?"라고 묻고는 진행)
- 대화 차례의 첫 말(say)이 물음으로 끝나면 next가 act여도 되묻기로 본다: 도구를 쓰지 않고 연구원님 답을 기다린다
- 위트는 선택 칸이고, 영어가 아니면 보여 주지도 기억하지도 않는다 (연구원님 결정 "위트는 영어 유지")

중간 보고 (M8, 연구원님 결정: 모델 판단 + 도구 6번)
- 범위를 크게 넓히기 전, 오래 걸릴 일 전, 유력한 곳에서 못 찾았을 때 모델이 찾은 것과 다음 길을 말하고 묻는다 (프롬프트)
- 그래도 한 작업에서 도구를 limits.checkin_steps번 쓰면 도구 없이 지금까지를 말하고 계속할지 묻게 한다 (reason "checkin").
  연구원님의 다음 말("응", "G:도 봐")이 같은 대화를 이어 간다

대화 기억 (core/context.py)
- 지난 작업(걸음, 도구 결과, 답)을 그대로 앞에 넣는다. 맥락 길이를 넘으면 오래된 것부터 줄인다
- 모델을 부르기 전마다 길이를 확인한다 (Ollama가 앞 대화를 몰래 자르지 않게)
- 이번 작업의 기록(transcript)을 돌려준다. 다음 작업이 이것을 기억한다

작은 모델을 위한 안전망 (원칙 4: 모델은 틀린다고 가정한다)
- 근거 확인: 파일이나 최신 정보를 다루는 이야기인데 이번 작업에서 도구를 한 번도 부르지 않고 답하면서, 출처가
  앞에서 확인한 자료에 없으면 한 번 되돌린다 (앞 대화에서 읽은 파일을 출처로 대면 그대로 받는다.
  도구를 불렀다가 차단·거절된 뒤의 답은 그 사실을 말하는 것이라 되돌리지 않는다)
- 출처 주소 확인 (M3): 답의 출처에 도구 결과(검색 결과, 읽은 페이지)에 나온 적 없는 주소가 있으면 한 번 되돌린다
  (작은 모델은 그럴듯한 논문 주소를 지어낸다)
- 답 안전망(guards.py): 각각 한 번만 되돌린다 (영어 답에만)
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from glados.config import LoopLimits
from glados.core import events as ev
from glados.core.context import (
    CHECK_MARK, STOP_MARK, TASK_RESERVE, Conversation, fit, grounded, local_texts, observation, size, unseen_urls,
)
from glados.core.events import Event
from glados.core.guards import Guard
from glados.core.llm import Cancelled, FormatError
from glados.core.prompts import PLANNING
from glados.core.schema import Answer, Plan, Step, parse_plan, plan_schema, step_schema
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
NUDGE_WEB = " For current information from the web (news, prices, releases, recent papers), use web.search."
NUDGE_SOURCES = (
    f"{CHECK_MARK} These sources are not in any tool result: {{urls}}. Cite only URLs that a tool returned, "
    "exactly as shown. If you have not found or read the page, search for it and read it first, "
    "or remove it from sources."
)
NUDGE_ACT = (
    f"{CHECK_MARK} Your plan is noted. Now take its first step: put a tool call in action and set done to false, "
    "or, if no tool is needed, set done to true and answer."
)
DENY_LIMIT = 3             # 한 작업에서 정책에 막혀도 되는 횟수 (넘으면 멈추고 보고)
HINT_SECRET = ("Do not save it anywhere else (no file, no command) and do not repeat it in your answer. "
               "Tell the Researcher it was not saved and suggest a password manager.")
HINT_SAFETY = (" GLaDOS's own folders (its program, settings, and logs) can never be changed, and commands there "
               "are read-only. Create files and run commands in another folder: {where}")
HINT_REPEAT = "\n[note] Same result as before. Unless you change the conditions a lot, answer with what you have."
# 같은 작업에서 똑같은 찾기·읽기를 다시 부르면 돌리지 않는다 (M7.5: "면허증" 찾기가 같은 검색 6번으로 12걸음을 씀).
# 상태를 바꾸거나 다시 볼 이유가 있는 도구(명령, 서버, 파일 고치기)는 빼고
NO_REPEAT = {"fs.search", "fs.inspect", "fs.read", "web.search", "web.fetch", "papers.search", "session.resume"}
NUDGE_PLANNED = (f"{CHECK_MARK} Plan noted. Goal: {{goal}} Carry it out one step at a time; if results show a better "
                 "way, change the plan. Your final answer must answer this goal as the Researcher asked it, not a "
                 "narrower question you made up; if you could do only part of it, say which part.")
NO_FACTS = "(none known)"
HINT_DONE_BEFORE = ("[note] You already made this exact call at step {step} of this task; its result is above. "
                    "It was not run again. Change the words, kind, or roots, use another tool, or answer with what you have.")
NUDGE_ENGLISH = (f"{CHECK_MARK} Write answer.fact in English. The answer is translated for the Researcher "
                 "automatically.")
CHECKIN = ("{mark} You have used {count} tools in this task. Do not call more tools now. Tell the Researcher what you "
           "checked and what you found so far, and ask in one short question whether to go on and how (for example a "
           "bigger place or another way).")
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
    reason: str = ""                     # max_steps | same_call | tool_errors | format (멈춤), ask (되물음, M7.8), checkin (중간 보고, M8)
    message: str = ""                    # 멈춘 이유 한 줄 (영어)
    trail: list[dict[str, Any]] = field(default_factory=list)       # 실행한 도구와 결과 앞부분 (작업 메모)
    transcript: list[Message] = field(default_factory=list)         # 이번 작업의 걸음과 관찰 (다음 작업이 기억)
    internal: Answer | None = None       # 모델이 쓴 답 (기억에 남는 것). 옮기지 않았으면 answer와 같다


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
    plan_first: bool = True,
    situation: str = "",
) -> TaskResult:
    """memory가 있으면 지난 대화를 길이에 맞게 넣는다 (없으면 history를 그대로).
    context_chars: 보낼 수 있는 전체 글자 수 (context.limit_chars). 없으면 줄이지 않는다.
    evidence_check: 글(요청 + 모델의 생각과 답)이 파일을 다루는지. 그렇다면 근거를 확인한다.
    answer_guards: 답을 받기 전 안전망 (guards.py). 각각 한 작업에서 한 번만 되돌린다.
    final_note: 시스템 메시지 맨 끝(도구 목록 뒤)에 붙일 지시 (언어 안내).
    translator, reply_lang: 모델의 영어 답을 연구원님의 언어로 옮겨 화면에 낸다 ("en"이면 옮기지 않음).
    cancelled: 연구원님이 멈췄는지 (TUI). 참이면 다음 걸음 전에 멈춘다.
    plan_first: 도구가 있으면 행동하기 전에 계획만 받는 호출을 먼저 한다 (M7.7).
    situation: 계획할 때 볼 이 컴퓨터의 지금 사실 (영어 몇 줄)."""
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
    if ctx.texts is None:      # 웹으로 나갈 글 검사(정책)가 볼 글: 이 컴퓨터의 도구가 돌려준 글, 연구원님이 쓴 글
        ctx.texts = lambda: local_texts([*hist, *task])
    nudge_evidence = NUDGE_EVIDENCE + (NUDGE_WEB if "web.search" in tools.names() else "")
    emit(Event(ev.START, {"request": request}))

    def outgoing(head: Message | None = None) -> list[Message]:
        nonlocal hist, task
        head = head or system
        if context_chars is not None:
            hist, task = fit(head, hist, task, context_chars, rerender)
        return [head, *hist, *task]

    def record(m: Message) -> None:
        task.append(m)
        transcript.append(m)

    last_key = ""
    same = 0
    done_calls: dict[str, int] = {}                     # 이 작업에서 이미 한 찾기·읽기 (호출 -> 걸음)
    errors: Counter[str] = Counter()
    trail: list[dict[str, Any]] = []
    last_obs: dict[str, str] = {}
    nudged = False
    sources_nudged = False     # 출처 주소 확인은 한 번만
    attempted = False          # 이번 작업에서 도구를 부르려 했는지 (차단·거절·오류도 포함)
    english_nudged = False     # 영어가 아닌 답은 한 번만 되돌린다 (M8.3)
    used = 0                   # 이번 작업에서 부른 도구 수 (중간 보고, M8)
    guards = list(answer_guards)
    fired: set[int] = set()    # 이미 되돌린 안전망
    planned_only = 0           # 계획만 쓰고 행동하지 않은 답 (한 번은 되돌려 준다)
    denied = 0                 # 정책에 막힌 횟수

    def send_back(step_no: int, key: str, nudge: str) -> None:
        """답을 받지 않고 한 번 되돌린다. 그 답은 화면에도 기억에도 남기지 않는다."""
        transcript.pop()
        emit(Event(ev.GUARD, {"step": step_no, "key": key, "message": t(key, "en")}))
        task.append({"role": "user", "content": nudge})

    def spoken(text: str) -> tuple[str, bool, bool]:
        """연구원님께 하는 말. 그 언어가 아니면 그때만 옮긴다 (M8). (보일 글, 옮겼는지, 옮기지 못했는지)."""
        if not text.strip() or translator is None or in_language(text, reply_lang):
            return text, False, False
        done = translator.translate(text, "", reply_lang)
        return (text, False, True) if done is None else (done[0], True, False)

    def deliver(said: Answer, sources: list[str]) -> Answer:
        """모델의 답을 화면에 낸다 (연구원님의 언어로 쓴 그대로, 아니면 옮겨서). 화면에 낸 답을 돌려준다.
        위트(wit)는 영어일 때만 (M6.3 연구원님: "위트는 영어로", M8.2: 아니면 뺀다)."""
        fact, translated, failed = spoken(said.fact)
        shown = Answer(fact=fact, wit=tidy(said).wit)
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
            said = step.answer
            transcript.append({"role": "assistant", "content": raw})
            shown = deliver(step.answer, sources)
        emit(Event(ev.STOP, {"reason": reason, "message": message, "detail": detail,
                             "answered": said is not None, "params": params}))
        return TaskResult("stopped", shown, sources, steps, reason, message, trail, transcript, internal=said)

    def show_plan(made: Plan) -> None:
        """대화 차례의 첫 말(say, 연구원님의 언어)과 계획(영어: 맥락 패널). M8: 계획은 옮기지 않는다."""
        steps = [p.strip() for p in made.plan if p.strip()][:MAX_PLAN]
        say, translated, failed = spoken(made.say.strip())
        emit(Event(ev.PLAN, {
            "step": 0, "say": say, "goal": made.goal.strip(), "plan": steps, "check": made.check.strip(),
            "lang": reply_lang, "translated": translated, "untranslated": failed,
            "original": {"say": made.say.strip()},
        }))

    def checkin(steps: int, count: int) -> TaskResult:
        """중간 보고 (M8): 도구 없이 지금까지 찾은 것을 말하고 계속할지 묻는다. 못 받으면 멈춤으로."""
        note = CHECKIN.format(mark=CHECK_MARK, count=count)
        msgs = outgoing() + [{"role": "user", "content": note}]
        try:
            got, raw = llm.chat_json(msgs, step_schema([]), Step.model_validate, retries=1)
        except FormatError:
            return stop("max_steps", steps, report=True, limit=steps)
        assert got.answer is not None
        record({"role": "assistant", "content": raw})
        shown = deliver(got.answer, list(got.sources))
        return TaskResult("done", shown, list(got.sources), steps, reason="checkin", trail=trail,
                          transcript=transcript, internal=got.answer)

    pending: tuple[Step, str] | None = None     # 계획 호출이 받은 첫 걸음 (계획 없이 바로 답했거나 걸음 모양의 답)
    current = 0                # 지금 걸음 (멈춤 보고용)
    try:
        if plan_first and tools.names():
            if cancelled():
                return stop("user", 0)
            facts = situation.strip() or NO_FACTS
            ahead = [system_prompt.rstrip(), tools.describe(), PLANNING.format(facts=facts)]   # 언어 안내는 늘 맨 끝
            planner: Message = {"role": "system", "content": "\n\n".join(ahead + ([final_note] if final_note else []))}
            try:
                made, raw = llm.chat_json(outgoing(planner), plan_schema(), parse_plan, retries=limits.format_retries)
            except FormatError:
                made, raw = None, ""                    # 계획 없이 예전처럼 (첫 걸음에 계획)
            if isinstance(made, Step):
                pending = (made, raw)
            elif isinstance(made, Plan) and (made.next == "ask" or (made.next == "act" and asks(made.say))):
                said = tidy(made.answer())                       # 묻고 끝, 답을 기다린다 (M7.8, M8.2)
                asked = Step(thought=made.goal, done=True, answer=said)
                record({"role": "assistant", "content": asked.model_dump_json(exclude={"plan", "action"})})
                shown = deliver(said, [])
                return TaskResult("done", shown, [], 0, reason="ask", trail=trail, transcript=transcript,
                                  internal=said)
            elif isinstance(made, Plan) and made.next == "answer":
                direct = Step(thought=made.goal, done=True, answer=tidy(made.answer()), sources=list(made.sources))
                pending = (direct, direct.model_dump_json(exclude={"plan", "action"}))
            elif isinstance(made, Plan):
                show_plan(made)
                task.append({"role": "assistant", "content": raw})
                goal = made.goal.strip() or "(see the plan)"
                task.append({"role": "user", "content": NUDGE_PLANNED.format(goal=goal.rstrip(".") + ".")})
                first = schema                          # 계획은 이미 있다: 걸음마다 바꿀 때만 쓴다

        for step_no in range(1, limits.max_steps + 1):
            current = step_no
            if cancelled():
                return stop("user", step_no - 1)
            if limits.checkin_steps and used >= limits.checkin_steps:
                return checkin(step_no - 1, used)                # 도구를 여러 번 썼다: 돌아와 말하고 묻는다
            if pending is not None:
                (step, raw), pending = pending, None
            else:
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
                    send_back(step_no, "guard.evidence", nudge_evidence)
                    continue
                unseen = [] if sources_nudged else unseen_urls(list(step.sources), [*hist, *task])
                if unseen:
                    sources_nudged = True
                    send_back(step_no, "guard.sources", NUDGE_SOURCES.format(urls=", ".join(unseen[:5])))
                    continue
                if not english_nudged and not in_language(step.answer.fact, "en"):
                    english_nudged = True                              # 전부 영어로 (M8.3)
                    send_back(step_no, "guard.english", NUDGE_ENGLISH)
                    continue
                found = None
                if in_language(step.answer.fact, "en"):                # 안전망 낱말 목록은 영어
                    for i, guard in enumerate(guards):
                        if i not in fired:
                            found = guard(said, bool(tools.names()))
                            if found is not None:
                                fired.add(i)
                                break
                if found is not None:
                    send_back(step_no, *found)
                    continue
                kept = tidy(step.answer)
                if kept is not step.answer:                      # 영어가 아닌 위트: 기억에서도 뺀다 (M8.2)
                    step = step.model_copy(update={"answer": kept})
                    transcript[-1] = task[-1] = {"role": "assistant",
                                                 "content": step.model_dump_json(exclude={"plan", "action"})}
                shown = deliver(kept, list(step.sources))
                return TaskResult("done", shown, list(step.sources), step_no, trail=trail, transcript=transcript,
                                  internal=kept)

            assert step.action is not None
            attempted = True
            used += 1
            call = ToolCall(step.action.tool, dict(step.action.args))
            emit(Event(ev.PROPOSAL, {"step": step_no, "tool": call.tool, "args": call.args}))

            key = json.dumps([call.tool, call.args], sort_keys=True, ensure_ascii=False, default=str)
            same = same + 1 if key == last_key else 1
            last_key = key
            if same >= limits.same_call_limit:
                return stop("same_call", step_no, report=True, tool=call.tool, count=same)
            if call.tool in NO_REPEAT and key in done_calls:
                result = HINT_DONE_BEFORE.format(step=done_calls[key])
                emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": result}))
                record(observation(call.tool, result))
                continue

            timeout = limits.remote_timeout_s if call.tool.startswith("remote.") else limits.local_timeout_s
            tool = tools.get(call.tool)
            if tool is not None and tool.timeout_s > timeout:
                timeout = tool.timeout_s                         # 오래 걸리는 도구 (fs.inspect: 파일마다 모델에게 물음)
            decision = policy.check(call, ctx)
            if decision.verdict == "ask" and tool is not None and tool.preview is not None:
                try:                                             # 승인 카드 (remote.run: GPU·커밋·규칙). 읽기만
                    call = replace(call, card=tools.preview(call, timeout_s=timeout, cancelled=cancelled))
                except ToolError as e:                           # 시작할 수 없는 일은 묻지 않고 오류로 돌려준다
                    errors[call.tool] += 1
                    result = f"Error: {e}"
                    emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": result, "key": e.key, "params": e.params}))
                    record(observation(call.tool, result))
                    if errors[call.tool] >= limits.tool_error_limit:
                        return stop("tool_errors", step_no, localized(e), report=True, tool=call.tool, count=errors[call.tool])
                    continue
            allowed, who, note = _decide(call, decision, ctx, approve)
            if who == "연구원님:거절" and tool is not None and tool.deny_note:
                note = tool.deny_note                            # remote.run: "명령이 맞지 않다" -> 무엇을 고칠지 묻기
            audit(call, decision, who)
            emit(Event(ev.DECISION, {
                "tool": call.tool, "verdict": decision.verdict, "rule": decision.rule,
                "reason": decision.reason, "decided_by": who,
                "key": decision.key, "params": dict(decision.params),
            }))

            if not allowed:
                result = note
                if decision.verdict == "deny":
                    denied += 1
                    if denied >= DENY_LIMIT:
                        emit(Event(ev.OBSERVATION, {"tool": call.tool, "text": result[:500]}))
                        record(observation(call.tool, result))
                        return stop("denied", step_no, report=True, count=denied)
            else:
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
                done_calls.setdefault(key, step_no)

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


def asks(text: str) -> bool:
    """연구원님께 묻는 말인가 (물음표로 끝남). 물었으면 답을 기다린다 (M8.2)."""
    return text.strip().rstrip("\"'”’)]").endswith(("?", "？"))


def tidy(answer: Answer) -> Answer:
    """영어가 아닌 위트는 뺀다 (연구원님 결정: 위트는 영어로, 맞을 때만). 바꿀 것이 없으면 그대로 돌려준다."""
    if answer.wit.strip() and not in_language(answer.wit, "en"):
        return Answer(fact=answer.fact, wit="")
    return answer


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
        hint = ""
        if d.rule.startswith("코드:안전장치"):       # GLaDOS 자기 폴더: 어디서 하면 되는지 알려 준다
            hint = HINT_SAFETY.format(where=ctx.workspace or ctx.places.home)
        if d.rule == "코드:비밀기억":                # 비밀은 다른 곳(파일·명령)에도 남기지 않게 (M6.2)
            return False, "정책", f"Blocked: {reason}. {HINT_SECRET}"
        return False, "정책", f"Blocked: {reason}.{hint} Find another way, or report that it cannot be done."
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
