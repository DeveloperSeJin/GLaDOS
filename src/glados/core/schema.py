"""출력 계약. 모델은 한 걸음마다 아래 JSON 하나만 낸다.

{"plan": ["어떻게 할지 한 걸음씩"], "thought": "판단 근거 한두 문장", "action": {"tool": "fs.search", "args": {...}}, "done": false}
{"thought": "...", "done": true, "answer": {"fact": "사실", "wit": "위트 한 문장"}, "sources": ["..."]}

계획 (M2.7, 연구원님 결정 "질문을 받으면 어떻게 할지 모델이 생각하게"): 도구가 있으면 첫 걸음에 plan이 꼭 있어야 한다.
무엇을 알아야 하고, 어디에 있고, 어떤 도구로 얻고, 어떻게 확인할지를 모델이 먼저 적고, 같은 답에서 첫 걸음을 뗀다.
뒤 걸음에서는 바꿀 때만.

순서와 필수 칸: plan(첫 걸음) -> thought -> action -> done. action은 꼭 쓰게 한다 (도구를 안 쓰면 null).
Ollama는 필수 칸을 먼저, 나머지는 뒤에 붙이는 문법으로 출력을 강제한다. action이 선택 칸이던 때 qwen3.5:4b가
계획을 쓴 뒤 done:false로 action을 빼먹는 일이 실제로 났다 (2026-11-11 연구원님 PC, 세 번 연속 형식 오류).
그래도 계획만 쓰고 행동을 안 하면 루프가 한 번 "이제 첫 걸음을 떼라"고 되돌린다.

계획 먼저 (M7.7, 연구원님 "움직이기 전에 어떻게 찾아야 하는지 계획을 먼저 세우는 방식으로"): 도구가 있으면 행동하기 전에
따로 한 번 계획만 받는다 (Plan). next로 셋 중 하나: ask(무엇을 원하는지 알 수 없음 -> 짐작하지 않고 되묻기,
M7.8), answer(인사, 아는 것 -> 바로 답), act(목표, 계획 단계, 확인 방법). 목표는 연구원님이 말한 범위 그대로 (M7.8). 그다음 걸음들은 계획을 따른다 (plan은 바꿀 때만). 계획 답이 형식을 못 맞추면 예전처럼 첫 걸음에 계획을 받는다.

대화 차례 (M8, 연구원님 "대화 방식을 크게 바꿔야"): 그 계획 호출이 곧 GLaDOS의 첫 말이다. say = 연구원님의 언어로
바로 하는 말 (answer면 답, ask면 되묻는 질문, act면 "어디서 무엇부터 하겠다"). 목표·계획·확인은 영어 (도구 쪽).
wit은 영어 한 문장, 맞을 때만 (연구원님 결정). act는 계획이 없어도 받는다 (형식 실패로 대화 차례를 잃지 않게).

Ollama `format`에 넣을 JSON 스키마는 지금 쓸 수 있는 도구 이름으로 좁힌다.
도구가 하나도 없으면 바로 답하도록 done은 true만 허용한다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Action(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Answer(BaseModel):
    fact: str
    wit: str = ""


class Progress(BaseModel):
    """나아가고 있나 (M9.4, Magentic-One 진행 장부의 is_progress_being_made: reason과 answer)."""
    reason: str = ""
    answer: bool = True


class Step(BaseModel):
    progress: Progress | None = None
    stage_done: Progress | None = None              # 지금 단계를 끝냈나, 무엇을 냈나 (M9.5, 단계가 있는 역할만)
    plan: list[str] = Field(default_factory=list)
    thought: str = ""
    action: Action | None = None
    done: bool
    answer: Answer | None = None
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Step":
        if self.done and self.answer is None:
            raise ValueError("done is true, so answer is required")
        stage_done = self.stage_done is not None and self.stage_done.answer     # 단계를 끝냈다는 걸음 (M9.5)
        if not self.done and self.action is None and not any(p.strip() for p in self.plan) and not stage_done:
            raise ValueError("done is false, so action is required")      # 계획만 쓴 답은 루프가 받아 되돌린다
        if self.done and not self.answer.fact.strip():  # type: ignore[union-attr]
            raise ValueError("answer.fact is empty")
        return self


MAX_PLAN = 6                  # 계획 단계 수 한도 (작은 모델이 목록을 끝없이 늘리지 않게)


class Plan(BaseModel):
    """대화 차례: 행동하기 전의 첫 말과 계획 (M7.7, M8)."""
    goal: str = ""
    next: Literal["ask", "answer", "act"]
    role: str = ""                                   # 일의 종류 (M9.2, 역할이 있을 때만 형식에 나온다)
    stage: str = ""                                  # 단계가 있는 역할에서 시작할 단계 (M9.5, 선택)
    plan: list[str] = Field(default_factory=list)
    check: str = ""
    say: str = ""
    wit: str = ""
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Plan":
        if self.next != "act" and not self.say.strip():
            raise ValueError(f'next is "{self.next}", so say is required')
        return self

    def answer(self) -> Answer:
        return Answer(fact=self.say.strip(), wit=self.wit.strip())


def parse_plan(data: dict[str, Any]) -> "Plan | Step":
    """계획 답. 걸음 모양이면(계획을 건너뛴 답, 시험의 가짜 모델) 그 걸음 그대로."""
    if "next" in data or "done" not in data:
        return Plan.model_validate(data)
    return Step.model_validate(data)

# 위트는 선택 칸 (M8.2): 필수면 작은 모델이 늘 채운다. Ollama는 선택 칸을 필수 칸 뒤에 두고 빼도 되게 한다
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"fact": {"type": "string"}, "wit": {"type": "string"}},
    "required": ["fact"],
}


def step_schema(tools: list[str] | list[tuple[str, dict[str, Any]]], plan: bool = False,
                staged: bool = False) -> dict[str, Any]:
    """tools: 도구 이름 목록, 또는 (이름, 인자 스키마) 목록. 인자 스키마가 있으면 도구별로 인자를 좁힌다.
    plan: 첫 걸음이면 계획을 꼭 쓰게 한다 (도구가 없으면 계획 없이 바로 답).
    staged: 단계가 있는 역할 (M9.5): progress 다음에 stage_done (지금 단계를 끝냈나)."""
    specs = [(t, {}) if isinstance(t, str) else (t[0], t[1]) for t in tools]
    if not specs:
        return {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "done": {"type": "boolean", "enum": [True]},
                "answer": ANSWER_SCHEMA,
                "sources": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thought", "done", "answer", "sources"],
        }
    actions = [
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": [name]},
                "args": params or {"type": "object"},
            },
            "required": ["tool", "args"],
        }
        for name, params in specs
    ]
    plan_schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PLAN}
    if plan:
        plan_schema["minItems"] = 1
    progress = {"type": "object", "properties": {"reason": {"type": "string"}, "answer": {"type": "boolean"}},
                "required": ["reason", "answer"]}
    stage = {"stage_done": progress} if staged else {}
    return {
        "type": "object",
        "properties": {
            "progress": progress,
            **stage,
            "plan": plan_schema,
            "thought": {"type": "string"},
            "action": {"anyOf": [*actions, {"type": "null"}]},
            "done": {"type": "boolean"},
            "answer": {"anyOf": [ANSWER_SCHEMA, {"type": "null"}]},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["progress"] + (["stage_done"] if staged else []) + (["plan"] if plan else [])
        + ["thought", "action", "done"],
    }


def plan_schema(roles: list[str] | None = None, stages: list[str] | None = None) -> dict[str, Any]:
    """대화 차례의 형식 (필수 칸 순서대로 쓰게 된다: 목표 -> 다음 -> (역할) -> 계획 -> 확인 -> 할 말 -> 출처 -> 위트).
    roles: 고를 수 있는 역할 이름 (M9.2). 있으면 next 다음에 role을 쓰게 한다.
    stages: 단계가 있는 역할의 단계 이름들 (M9.5). 있으면 선택 칸 stage (어디서 시작할지)."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "next": {"type": "string", "enum": ["ask", "answer", "act"]},
            "plan": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PLAN},
            "check": {"type": "string"},
            "say": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
            "wit": {"type": "string"},
        },
        "required": ["goal", "next", "plan", "check", "say", "sources"],      # wit은 선택 (M8.2)
    }
    if roles:
        props = schema["properties"]
        ordered = {"goal": props["goal"], "next": props["next"], "role": {"type": "string", "enum": list(roles)}}
        ordered.update({k: v for k, v in props.items() if k not in ordered})
        schema["properties"] = ordered
        schema["required"] = ["goal", "next", "role", "plan", "check", "say", "sources"]
        if stages:
            schema["properties"]["stage"] = {"type": "string", "enum": list(dict.fromkeys(stages))}
    return schema
