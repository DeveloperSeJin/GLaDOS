"""손: GLaDOS가 쓰는 도구.

모델이 보는 도구 이름은 OS와 상관없이 같다. 구현만 hands/<os>/에 둔다.
그 OS에 없는 도구는 목록에서 빠지고, GLaDOS는 "이 컴퓨터에서는 못 합니다"라고 답한다.

v0에서 손은 이 인터페이스로 붙는다. 나중에 MCP 서버로 옮겨도 같은 인터페이스를 쓴다.
M1에는 도구가 없다 (M2 파일, M3 웹, M4·M5 서버).
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from glados.safety.policy import ToolCall


class ToolError(Exception):
    """도구 실행 실패. 루프가 관찰로 받아 모델에게 돌려준다."""


@dataclass
class Tool:
    name: str
    summary: str                                   # 이름 옆 한 줄 설명
    params: dict[str, Any] = field(default_factory=dict)  # 인자 JSON 스키마
    run: Callable[[dict[str, Any]], str] = lambda args: ""
    timeout_s: int = 60


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            if t.name in self._tools:
                raise ValueError(f"도구 이름이 겹칩니다: {t.name}")
            self._tools[t.name] = t

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe(self) -> str:
        if not self._tools:
            return "지금 쓸 수 있는 도구가 없습니다. 도구를 부르지 말고 아는 범위에서 바로 답하세요."
        lines = ["쓸 수 있는 도구:"]
        lines += [f"- {t.name}: {t.summary}" for t in self._tools.values()]
        return "\n".join(lines)

    def run(self, call: ToolCall, timeout_s: int | None = None) -> str:
        tool = self._tools.get(call.tool)
        if tool is None:
            raise ToolError(f"이 컴퓨터에서는 {call.tool}을(를) 쓸 수 없습니다")
        limit = timeout_s or tool.timeout_s
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["out"] = tool.run(dict(call.args))
            except Exception as e:  # 도구 안의 모든 실패는 ToolError로
                box["err"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(limit)
        if th.is_alive():
            raise ToolError(f"{call.tool} 실행이 {limit}초를 넘었습니다")
        if "err" in box:
            raise ToolError(f"{call.tool} 실패: {box['err']}")
        return str(box.get("out", ""))


def load_hands(os_name: str) -> ToolRegistry:
    """공통 손 + 이 OS의 손을 모은다."""
    tools: list[Tool] = []
    common = importlib.import_module("glados.hands.common")
    tools += common.tools()
    try:
        mod = importlib.import_module(f"glados.hands.{os_name}")
        tools += mod.tools()
    except ModuleNotFoundError:
        pass
    return ToolRegistry(tools)
