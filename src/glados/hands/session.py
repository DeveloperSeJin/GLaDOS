"""대화를 잇고 기억하는 손 (M6).

- session.resume(query?): 지난 대화를 불러온다. 이번 작업에는 요약을 돌려주고, 작업이 끝나면 앱이 그 세션의
  턴을 기억에 넣어 이어 간다 (작업 중에 기억을 바꾸지 않는다: 이번 작업의 맥락이 흔들리지 않게).
- memory.note(text): "기억해"라고 한 것을 MEMORY.md에 한 줄 남긴다. 매번 승인 (승인 상자에 저장할 글을 보여 줌).
"""

from __future__ import annotations

from typing import Any

from glados.hands import HandContext, Tool, tool_error
from glados.i18n import t
from glados.memory.notes import clean
from glados.memory.sessions import recap
from glados.safety.privacy import secret_in_note


def session_tools(hctx: HandContext) -> list[Tool]:
    tools: list[Tool] = []

    if hctx.sessions is not None:
        def resume(args: dict[str, Any]) -> str:
            store = hctx.sessions
            query = str(args.get("query") or "").strip()
            info = store.find(query, exclude=hctx.session_id)
            if info is None:
                recent = store.recent(5, hctx.session_id)
                if not recent:
                    raise tool_error("tool.resume_none")
                raise tool_error("tool.resume_nomatch", query=query,
                                 sessions="; ".join(f"{s.when()} {s.title}" for s in recent))
            hctx.resume_to = info.id
            return (recap(info, store.turns(info.id)) +
                    "\nThe conversation continues from it after this task. Answer from this summary; "
                    "read files or logs again if you need details.")

        tools.append(Tool("session.resume",
                          "continue an earlier conversation (query: words from it; empty = latest)",
                          {"type": "object", "properties": {"query": {"type": "string"}}},
                          resume, timeout_s=15))

    if hctx.notes is not None:
        def text_of(args: dict[str, Any]) -> str:
            text = clean(args.get("text") or "")
            if not text:
                raise tool_error("tool.note_empty")
            return text

        def note(args: dict[str, Any]) -> str:
            text = text_of(args)
            if secret_in_note(text):                     # 정책이 막지만, 한 번 더 (M6.2)
                raise tool_error("tool.note_secret")
            n, item, new = hctx.notes.add(text)
            return f"Saved as note {n} in MEMORY.md: {item}" if new else f"Already in memory as note {n}: {item}"

        def preview(args: dict[str, Any]) -> list[tuple[str, str]]:
            return [("fact", text_of(args)), ("dim", t("note.card_where", path=str(hctx.notes.path)))]

        tools.append(Tool("memory.note",
                          "save a line the Researcher asked you to remember, in their own words and language",
                          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                          note, timeout_s=10, preview=preview,
                          deny_note="The Researcher did not want this saved. Do not save it; continue without it."))
    return tools
