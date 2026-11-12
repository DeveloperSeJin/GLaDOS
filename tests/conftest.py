from __future__ import annotations

import json
from typing import Any

import pytest

from glados.config import default_text, parse_policy_text
from glados.safety.locations import Places
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

ALL_TOOLS = frozenset({
    "fs.search", "fs.inspect", "fs.read", "fs.write_new", "fs.edit", "fs.trash", "shell.run",
    "web.search", "web.fetch", "remote.gpu", "remote.log", "remote.exec", "remote.run", "remote.stop", "remote.projects",
    "git.snapshot", "ask.user", "session.resume", "memory.note", "papers.search",
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


REAL_SEARCH_BY_OS: dict[str, str] = {}      # OS별 웹 검색 기본값 (시험마다 비워 두기 전의 것)


@pytest.fixture(autouse=True)
def _no_default_search(monkeypatch):
    """시험이 만드는 설정 폴더는 웹 검색 없이 (실제 기본값은 OS마다 다름: Windows·맥 Tavily, 리눅스 SearXNG).
    OS별 기본값은 test_config에서 따로 본다."""
    from glados import config
    global REAL_SEARCH_BY_OS
    REAL_SEARCH_BY_OS = REAL_SEARCH_BY_OS or dict(config.SEARCH_BY_OS)
    monkeypatch.setattr(config, "SEARCH_BY_OS", {})


@pytest.fixture(autouse=True)
def _korean_screen():
    """화면 언어는 전역이라, 영어로 바꾼 시험이 다른 시험에 새지 않게."""
    from glados import i18n
    i18n.set_language("ko")
    yield
    i18n.set_language("ko")


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
        if isinstance(item, BaseException):        # LLMError, 멈춤(Cancelled)
            raise item
        return validate(item), json.dumps(item, ensure_ascii=False)

    def has_model(self) -> bool:
        return True


class StaticGlossary:
    """모델 없이 정해 둔 낱말 -> 영어 키워드 (찾기 시험용). 진짜 Glossary와 같은 english()를 갖는다."""

    WORDS: dict[str, tuple[str, ...]] = {
        "계약": ("contract", "agreement"), "계약서": ("contract", "agreement"),
        "외주개발": ("outsourcing", "development"), "임대": ("lease", "rental"),
        "회의록": ("minutes", "meeting", "notes"), "예전메모": ("old", "memo", "note"),
        "학위논문": ("thesis", "dissertation", "paper"), "논문": ("paper", "thesis"), "논문초안": ("paper", "draft"),
        "다운로드": ("download", "downloads"), "폴더": ("folder",), "문서": ("documents", "document"),
        "바탕화면": ("desktop",), "홈": ("home",), "노트": ("note", "notebook"), "메모": ("memo", "note"),
        "발표": ("presentation", "slides"), "실험": ("experiment",), "계획": ("plan",), "요약": ("summary",),
        "최종": ("final",), "설명": ("description",), "분석": ("analysis",), "시험": ("test", "exam"),
        "초안": ("draft",), "긴글": ("long", "text"), "그림": ("picture", "image"), "보고서": ("report",),
    }

    def __init__(self, words: dict[str, tuple[str, ...]] | None = None):
        self.words = dict(self.WORDS if words is None else words)
        self.asked: list[str] = []

    def english(self, tokens, deadline=None):
        import unicodedata

        out = {}
        for tok in tokens:
            key = unicodedata.normalize("NFC", tok).casefold()
            self.asked.append(key)
            if key in self.words:
                out[key] = self.words[key]
        return out


def answer(fact: str = "Done.", wit: str = "", sources: list[str] | None = None) -> dict[str, Any]:
    return {"thought": "Answer now.", "done": True, "answer": {"fact": fact, "wit": wit}, "sources": sources or []}


def act(tool: str, **args: Any) -> dict[str, Any]:
    return {"thought": f"Call {tool}.", "done": False, "action": {"tool": tool, "args": args}}
