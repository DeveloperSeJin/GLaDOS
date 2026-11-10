"""출력 계약. 모델은 한 걸음마다 아래 JSON 하나만 낸다.

{"thought": "판단 근거 한두 문장", "action": {"tool": "fs.search", "args": {...}}, "done": false}
{"thought": "...", "done": true, "answer": {"fact": "사실", "wit": "위트 한 문장"}, "sources": ["..."]}

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
    thought: str = ""
    action: Action | None = None
    done: bool
    answer: Answer | None = None
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Step":
        if self.done and self.answer is None:
            raise ValueError("done이 true이면 answer가 있어야 합니다")
        if not self.done and self.action is None:
            raise ValueError("done이 false이면 action이 있어야 합니다")
        if self.done and not self.answer.fact.strip():  # type: ignore[union-attr]
            raise ValueError("answer.fact가 비어 있습니다")
        return self


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"fact": {"type": "string"}, "wit": {"type": "string"}},
    "required": ["fact", "wit"],
}


def step_schema(tool_names: list[str]) -> dict[str, Any]:
    if not tool_names:
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
    action = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": list(tool_names)},
            "args": {"type": "object"},
        },
        "required": ["tool", "args"],
    }
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "action": {"anyOf": [action, {"type": "null"}]},
            "done": {"type": "boolean"},
            "answer": {"anyOf": [ANSWER_SCHEMA, {"type": "null"}]},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["thought", "done"],
    }
