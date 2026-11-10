from __future__ import annotations

import json
from typing import Any

import pytest

from glados.config import default_text, parse_policy_text
from glados.safety.locations import Places
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

ALL_TOOLS = frozenset({
    "fs.search", "fs.read", "fs.write_new", "fs.edit", "fs.trash", "shell.run",
    "web.search", "web.fetch", "remote.gpu", "remote.log", "remote.exec", "git.snapshot", "ask.user",
})


def make_places(os_name: str, **kw: Any) -> Places:
    if os_name == "windows":
        base = dict(
            os="windows",
            home="C:\\Users\\User",
            glados_home="C:\\Users\\User\\code\\.glados",
            source_dirs=("C:\\Users\\User\\code\\glados",),
            env=(("APPDATA", "C:\\Users\\User\\AppData\\Roaming"), ("USERPROFILE", "C:\\Users\\User")),
        )
    elif os_name == "mac":
        base = dict(os="mac", home="/Users/sejin", glados_home="/Users/sejin/.glados",
                    source_dirs=("/Users/sejin/code/glados",))
    else:
        base = dict(os="linux", home="/home/sejin", glados_home="/home/sejin/.glados",
                    source_dirs=("/home/sejin/code/glados",))
    base.update(kw)
    base["resolve_links"] = False
    return Places(**base)


def base_layers() -> list[tuple[str, dict[str, Any]]]:
    return [("기본", parse_policy_text(default_text("policy.yaml"), "기본"))]


@pytest.fixture
def policy() -> Policy:
    return Policy(base_layers())


def make_ctx(os_name: str = "linux", places_kw: dict | None = None, **kw: Any) -> PolicyContext:
    kw.setdefault("registered_tools", ALL_TOOLS)
    return PolicyContext(places=make_places(os_name, **(places_kw or {})), **kw)


def check(policy: Policy, os_name: str, tool: str, ctx: PolicyContext | None = None, **args: Any) -> Decision:
    return policy.check(ToolCall(tool, args), ctx or make_ctx(os_name))


class FakeLLM:
    """정해진 순서대로 걸음을 돌려주는 가짜 모델."""

    def __init__(self, steps: list[Any]):
        self.steps = list(steps)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        self.calls.append((list(messages), schema))
        item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return validate(item), json.dumps(item, ensure_ascii=False)

    def has_model(self) -> bool:
        return True


def answer(fact: str = "끝났습니다.", wit: str = "", sources: list[str] | None = None) -> dict[str, Any]:
    return {"thought": "답한다", "done": True, "answer": {"fact": fact, "wit": wit}, "sources": sources or []}


def act(tool: str, **args: Any) -> dict[str, Any]:
    return {"thought": f"{tool}를 부른다", "done": False, "action": {"tool": tool, "args": args}}
