"""답 점검 (M9.3): 답을 연구원님께 내기 전에 모델이 한 번 더 본다. 낱말 목록(정규식) 안전망과 규칙 덧대기를 대신한다.

연구원님 "땜질 말고 근본적인 해결책" -> 다른 하네스를 공식 소스로 보고 만든다:
- smolagents (huggingface/smolagents src/smolagents/agents.py): final_answer_checks. 최종 답을 받기 전에 점검 함수들을
  돌리고, 하나라도 실패하면 AgentError를 그 걸음의 기록(action_step.error)에 남기고 루프를 계속한다.
  여기서도 걸린 답과 점검 결과를 이번 작업의 대화에 남겨 모델이 보고 고치게 한다 (기억·화면에는 남기지 않음)
- Magentic-One (microsoft/autogen _magentic_one/_prompts.py ORCHESTRATOR_PROGRESS_LEDGER_PROMPT): 대화 끝에 질문
  몇 개를 붙이고, 질문마다 {"reason": string, "answer": boolean} JSON으로 받는다. 키가 빠지면 다시 받는다.
  같은 대화(같은 시스템 메시지와 기록) 뒤에 붙이므로 Ollama가 앞부분을 다시 계산하지 않는다
- OpenAI Agents SDK output guardrails: 답을 보고 걸리면 멈춘다 (tripwire). 여기서는 멈추지 않고 되돌린다 (smolagents 쪽)
- Anthropic "Building effective agents" evaluator-optimizer: 만드는 쪽과 평가하는 쪽을 나누고, 평가는 기준이 분명할 때

기준은 셋. 답마다 같은 기준으로 판단하고, 판단은 모델이 한다 (낱말이 아니라 이번 작업의 결과와 대화를 보고):
  answers_request    요청한 것에, 요청한 범위로 답했나 (하겠다는 말만, 좁혀 바꾼 질문, 보여 달란 것을 빼먹음은 아님)
  claims_supported   파일·데이터·결과·이 컴퓨터·지금 세상에 대한 말이 이번 결과나 앞 대화에 있나
  results_plausible  결과를 말한다면 그 방법으로 나올 수 있는 값인가 (p = 1, 통계량 0, 모두 같은 값, 빈 출력)

점검이 실패하면(형식 오류·모델 오류) 답을 그대로 낸다: 점검은 답을 막는 문이 아니라 고칠 기회다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from glados.core.context import CHECK_MARK
from glados.core.llm import FormatError, LLMError

KEYS = ("answers_request", "claims_supported", "results_plausible")

CHECK_PROMPT = """\
{mark} Before your draft answer above goes to the Researcher, check it. The Researcher's request: {request}
Answer these questions about the draft, each with a short reason:
- answers_request: Does the draft give the Researcher what they asked for, at the scope they asked? A short question \
back counts when the request is unclear or lacks a needed detail. A draft that only says what it will do next does \
not count. A narrower question swapped in for the request does not count. If they asked to see something (a table, a \
list, numbers), it must be in the draft itself.
- claims_supported: Is every specific claim about files, data, results, this computer, or the current world (news, \
prices, weather, versions, recent papers) backed by a tool result in this conversation or by the Researcher's own \
words? General knowledge needs no tool. A draft that says what it could not check is fine.
- results_plausible: If the draft reports results, could the method used give them? A p-value of exactly 1 or 0, a \
test statistic of exactly 0, every number the same, or an empty output usually means the method or the input was \
wrong, unless the draft says so. True if the draft reports no results.
Reply with JSON only: {{"answers_request": {{"reason": string, "answer": boolean}}, "claims_supported": \
{{"reason": string, "answer": boolean}}, "results_plausible": {{"reason": string, "answer": boolean}}}}"""

# 만드는 쪽(걸음의 출력 규칙)에 알려 주는 같은 기준 (평가 기준을 미리 알면 첫 답이 덜 걸린다). 판단은 점검이 한다
RUBRIC = ("Before your answer is shown, it is checked: does it answer what the Researcher asked, at the scope they "
          "asked (anything they asked to see goes in answer.fact itself); is every claim about files, data, results, "
          "this computer, or the current world backed by a tool result or the Researcher's words; and could the "
          "method used give the reported results. Say what you could not check.")

FEEDBACK = (
    f"{CHECK_MARK} Your draft was not given to the Researcher. A check found:\n{{problems}}\n"
    "Fix it: use a tool to check what is not backed, recheck the method if a result is implausible, or answer only "
    "what the results support and say what you could not check. Then answer again."
)


class Item(BaseModel):
    reason: str = ""
    answer: bool


class Verdict(BaseModel):
    answers_request: Item
    claims_supported: Item
    results_plausible: Item

    def failed(self) -> list[tuple[str, str]]:
        return [(k, getattr(self, k).reason.strip()) for k in KEYS if not getattr(self, k).answer]


def ledger_schema(keys: tuple[str, ...]) -> dict[str, Any]:
    """질문마다 {"reason": string, "answer": boolean} (Magentic-One LedgerEntry)."""
    item = {"type": "object", "properties": {"reason": {"type": "string"}, "answer": {"type": "boolean"}},
            "required": ["reason", "answer"]}
    return {"type": "object", "properties": {k: item for k in keys}, "required": list(keys)}


def check_schema() -> dict[str, Any]:
    return ledger_schema(KEYS)


@dataclass
class Outcome:
    """점검 결과. passed가 거짓이면 feedback을 이번 작업의 대화에 넣고 다시 답하게 한다."""

    passed: bool
    failed: list[tuple[str, str]] = field(default_factory=list)
    feedback: str = ""
    error: str = ""


def ledger(llm: Any, messages: list[dict[str, str]], prompt: str, keys: tuple[str, ...],
           retries: int = 1) -> dict[str, Item] | None:
    """대화 뒤에 질문을 붙여 질문마다 {"reason", "answer"}를 받는다 (Magentic-One 진행 장부). 못 받으면 None."""
    def parse(data: dict[str, Any]) -> dict[str, Item]:
        missing = [k for k in keys if not isinstance(data.get(k), dict)]
        if missing:                                    # 키가 빠지면 다시 받는다 (Magentic-One required_keys)
            raise ValueError(f"missing {', '.join(missing)}")
        return {k: Item.model_validate(data[k]) for k in keys}

    try:
        got, _ = llm.chat_json([*messages, {"role": "user", "content": prompt}], ledger_schema(keys), parse,
                               retries=retries, temperature=0.0)
    except (LLMError, TypeError, ValueError):
        return None
    return got


# 단계 조건 확인 (M9.5): 모델이 단계를 끝냈다고 하면 같은 대화로 그 단계의 조건을 묻는다
GATE_PROMPT = """\
{mark} You say stage {k}/{n} ({name}) is done: {said}
This stage is done when: {gate}
Answer each with a short reason:
- met: Is that true, judging by the tool results and the Researcher's words in this conversation?{approve}
Reply with JSON only, each key as {{"reason": string, "answer": boolean}}."""
GATE_APPROVE = ("\n- approved: Has the Researcher stated or agreed to this {name} in their own words in this "
                "conversation (asking for exactly this counts)?")


@dataclass
class Gate:
    met: bool
    reason: str = ""
    approved: bool = True
    error: bool = False


def check_gate(llm: Any, messages: list[dict[str, str]], k: int, n: int, name: str, gate: str, said: str,
               approve: bool) -> Gate:
    prompt = GATE_PROMPT.format(mark=CHECK_MARK, k=k, n=n, name=name, said=said.strip() or "-", gate=gate,
                                approve=GATE_APPROVE.format(name=name) if approve else "")
    got = ledger(llm, messages, prompt, ("met", "approved") if approve else ("met",))
    if got is None:                               # 확인이 깨지면 모델 말대로 (답 점검과 같다)
        return Gate(True, error=True, approved=not approve)
    return Gate(got["met"].answer, got["met"].reason.strip(), got["approved"].answer if approve else True)


@dataclass
class Checker:
    """대화(시스템 메시지 + 기록 + 이번 작업 + 답 초안) 뒤에 점검 질문을 붙여 한 번 묻는다."""

    llm: Any
    retries: int = 1                           # 형식이 틀리면 다시 받는 횟수 (Magentic-One은 JSON 재시도)

    def __call__(self, messages: list[dict[str, str]], request: str) -> Outcome:
        prompt = CHECK_PROMPT.format(mark=CHECK_MARK, request=request.strip())
        try:
            verdict, _ = self.llm.chat_json([*messages, {"role": "user", "content": prompt}], check_schema(),
                                            Verdict.model_validate, retries=self.retries, temperature=0.0)
        except (FormatError, LLMError) as e:
            return Outcome(True, error=str(e))
        failed = verdict.failed()
        if not failed:
            return Outcome(True)
        problems = "\n".join(f"- {key}: {reason or 'no'}" for key, reason in failed)
        return Outcome(False, failed, FEEDBACK.format(problems=problems))
