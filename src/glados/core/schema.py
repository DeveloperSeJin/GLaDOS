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

Ollama `format`에 넣을 JSON 스키마는 지금 쓸 수 있는 도구 이름으로 좁힌다.
도구가 하나도 없으면 바로 답하도록 done은 true만 허용한다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Action(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Answer(BaseModel):
    fact: str
    wit: str = ""


class Step(BaseModel):
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
        if not self.done and self.action is None and not any(p.strip() for p in self.plan):
            raise ValueError("done is false, so action is required")      # 계획만 쓴 답은 루프가 받아 되돌린다
        if self.done and not self.answer.fact.strip():  # type: ignore[union-attr]
            raise ValueError("answer.fact is empty")
        return self


MAX_PLAN = 6                  # 계획 단계 수 한도 (작은 모델이 목록을 끝없이 늘리지 않게)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"fact": {"type": "string"}, "wit": {"type": "string"}},
    "required": ["fact", "wit"],
}


def step_schema(tools: list[str] | list[tuple[str, dict[str, Any]]], plan: bool = False) -> dict[str, Any]:
    """tools: 도구 이름 목록, 또는 (이름, 인자 스키마) 목록. 인자 스키마가 있으면 도구별로 인자를 좁힌다.
    plan: 첫 걸음이면 계획을 꼭 쓰게 한다 (도구가 없으면 계획 없이 바로 답)."""
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
    return {
        "type": "object",
        "properties": {
            "plan": plan_schema,
            "thought": {"type": "string"},
            "action": {"anyOf": [*actions, {"type": "null"}]},
            "done": {"type": "boolean"},
            "answer": {"anyOf": [ANSWER_SCHEMA, {"type": "null"}]},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": (["plan"] if plan else []) + ["thought", "action", "done"],
    }
