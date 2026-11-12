"""손: GLaDOS가 쓰는 도구.

모델이 보는 도구 이름은 OS와 상관없이 같다. 구현만 hands/<os>/에 둔다.
그 OS에 없는 도구는 목록에서 빠지고, GLaDOS는 "이 컴퓨터에서는 못 합니다"라고 답한다.

v0의 손은 엔진 안에 있다 (연구원님 결정, 2026-11-10). 외부 프로그램과 이어야 할 때
같은 인터페이스를 MCP 서버로 감싼다. 정책 검사는 어느 쪽이든 엔진이 실행 직전에 한다.

엔진 안쪽은 영어로 통일한다 (M2.5): 도구 설명, 결과, 오류 글은 모두 영어다 (모델이 영어로 생각함).
오류는 키도 함께 담아서, 화면에 보여 줄 때는 설정한 화면 언어로 바꾼다 (i18n "tool.*").
"""

from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from glados.core.textfix import clean, clean_any
from glados.i18n import KeyedError, t
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places
from glados.safety.policy import ToolCall

if TYPE_CHECKING:
    from glados.core.translate import Glossary


class ToolError(KeyedError):
    """도구 실행 실패. 루프가 관찰로 받아 모델에게 돌려준다 (글은 영어).
    키(i18n)가 있으면 화면에서는 설정한 언어로 보여 준다."""


def tool_error(key: str, **params: Any) -> ToolError:
    """i18n 키로 만드는 도구 오류: 글은 영어(모델용), 키와 값은 화면용. 값의 깨진 글자는 고친다 (M6.2)."""
    params = {k: clean_any(v) for k, v in params.items()}
    return ToolError(t(key, "en", **params), key, **params)


@dataclass
class Tool:
    name: str
    summary: str                                           # 이름 옆 한 줄 설명 (영어)
    params: dict[str, Any] = field(default_factory=dict)   # 인자 JSON 스키마
    run: Callable[[dict[str, Any]], str] = lambda args: ""
    timeout_s: int = 60
    # 승인 상자에 보여 줄 카드 [(모양, 글)] (화면 언어). 승인이 필요할 때만 실행 전에 부른다 (remote.run: GPU·커밋·규칙).
    # 읽기만 해야 한다. 오류를 내면 승인을 묻지 않고 도구 오류로 돌아간다
    preview: Callable[[dict[str, Any]], list[tuple[str, str]]] | None = None
    # 연구원님이 승인 상자에서 거절했을 때 모델에게 돌려줄 글 (없으면 기본: 다른 길을 찾거나 그대로 보고)
    deny_note: str = ""

    def signature(self) -> str:
        props = self.params.get("properties", {})
        required = set(self.params.get("required", []))
        args = ", ".join(k if k in required else f"{k}?" for k in props)
        return f"{self.name}({args})"


@dataclass
class HandContext:
    """손이 쓰는 이 컴퓨터의 정보."""

    places: Places
    home: Path                                   # GLADOS_HOME
    search_default: list[str] = field(default_factory=list)
    checkpoints: Checkpoints | None = None
    task_id: str = ""                            # 지금 작업 (체크포인트를 묶는 데 씀)
    user_dirs: dict[str, tuple[str, ...]] = field(default_factory=dict)   # osinfo.user_folders(), 비면 홈 아래 이름으로
    command_timeout_s: float = 55                # shell.run 시간 제한 (루프의 도구 시간 제한보다 조금 짧게)
    glossary: "Glossary | None" = None           # 이름 속 낱말을 영어로 (찾기에서 영어끼리 비교). 없으면 글자 그대로만
    llm: Any = None                              # 손이 모델에게 물을 때 (fs.inspect: 파일마다 질문). 없으면 그 도구는 오류
    model_name: str = ""                         # 답 캐시를 모델마다 따로 두려고
    web: Any = None                              # config.WebConfig ([web]). 없으면 기본값 (검색 없음)
    egress: Any = None                           # hands.web.Egress: 이 세션의 외부 전송 기록. 없으면 웹 손이 만든다
    emit: Callable[[Any], None] | None = None    # 손이 알리는 이벤트 (외부 전송). 작업마다 앱이 넣는다
    env: dict[str, str] | None = None            # API 키를 찾을 환경 변수 (없으면 os.environ. 시험에서 바꿈)
    servers: list[Any] = field(default_factory=list)   # config.Server 목록 (servers.toml). 비면 원격 손 없음 (M4)
    hub: Any = None                              # hands.remote.Hub: 서버 연결들 (앱이 끝날 때 닫는다)
    gpu_watch: Any = None                        # safety.fair.GpuWatch: 서버 GPU를 본 기록 (이 세션)
    runner: Any = None                           # hands.runner.Runner: 서버 실행·대기 줄·배경 감시 (M5)
    # 대화 세션 (M6): 저장소, 지금 세션, 작업 중 session.resume이 고른 세션 (작업이 끝나면 앱이 불러온다)
    sessions: Any = None                         # memory.sessions.SessionStore. 없으면 session.resume 없음
    session_id: str = ""
    resume_to: str = ""
    notes: Any = None                            # memory.notes.Notes (MEMORY.md). 없으면 memory.note 없음
    embedder: Any = None                         # core.llm.Embedder: 뜻으로 찾기의 질문 벡터 (M7.2). 없으면 낱말로만


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            if tool.name in self._tools:
                raise ValueError(f"도구 이름이 겹칩니다: {tool.name}")
            self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[tuple[str, dict[str, Any]]]:
        """(이름, 인자 스키마) 목록. 출력 스키마를 도구별로 좁히는 데 쓴다."""
        return [(tool.name, tool.params) for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe(self) -> str:
        if not self._tools:
            return "No tools are available right now. Do not call tools; answer directly from what you know."
        lines = ["Tools (a ? after an argument means optional):"]
        lines += [f"- {tool.signature()}: {tool.summary}" for tool in self._tools.values()]
        return "\n".join(lines)

    def run(self, call: ToolCall, timeout_s: int | None = None,
            cancelled: Callable[[], bool] | None = None) -> str:
        """도구를 따로 스레드에서 돌린다 (시간 제한). cancelled가 참이 되면 기다리지 않고 Cancelled를 낸다
        (TUI의 멈춤. 도구 스레드는 결과를 버리고 혼자 끝난다)."""
        tool = self._tools.get(call.tool)
        if tool is None:
            raise tool_error("tool.unavailable", tool=call.tool)
        out = str(self._threaded(call.tool, tool.run, dict(call.args), timeout_s or tool.timeout_s, cancelled))
        return clean(out)                # 깨진 글자(Windows 파일 이름 등)가 기록·기억·모델로 가지 않게 (M6.2)

    def preview(self, call: ToolCall, timeout_s: int | None = None,
                cancelled: Callable[[], bool] | None = None) -> list[tuple[str, str]]:
        """승인 상자에 보여 줄 카드 (도구에 preview가 없으면 빈 목록)."""
        tool = self._tools.get(call.tool)
        if tool is None or tool.preview is None:
            return []
        out = self._threaded(call.tool, tool.preview, dict(call.args), timeout_s or tool.timeout_s, cancelled)
        return list(out or [])

    def _threaded(self, name: str, fn: Callable[[dict[str, Any]], Any], args: dict[str, Any], limit: int,
                  cancelled: Callable[[], bool] | None) -> Any:
        from glados.core.llm import Cancelled

        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["out"] = fn(args)
            except ToolError as e:
                box["err"] = e
            except Exception as e:  # 도구 안의 모든 실패는 ToolError로
                box["err"] = tool_error("tool.failed", tool=name, detail=f"{type(e).__name__}: {e}")
            except BaseException as e:  # 멈춤(Cancelled): 부른 쪽에서 다시 낸다
                box["base"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        deadline = time.monotonic() + limit
        while th.is_alive() and time.monotonic() < deadline:
            th.join(min(0.1, max(0.0, deadline - time.monotonic())) if cancelled else limit)
            if cancelled is not None and cancelled():
                raise Cancelled()
        if th.is_alive():
            raise tool_error("tool.timeout", tool=name, seconds=limit)
        if "base" in box:
            raise box["base"]
        if "err" in box:
            raise box["err"]
        return box.get("out", "")


def load_hands(os_name: str, hctx: HandContext | None = None) -> ToolRegistry:
    """공통 손 + 이 OS의 손을 모은다. hctx가 없으면 도구 없이."""
    if hctx is None:
        return ToolRegistry()
    tools: list[Tool] = []
    tools += importlib.import_module("glados.hands.common").tools(hctx)
    try:
        mod = importlib.import_module(f"glados.hands.{os_name}")
        tools += mod.tools(hctx)
    except ModuleNotFoundError:
        pass
    return ToolRegistry(tools)
