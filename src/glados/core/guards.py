"""답을 받기 전 안전망 (원칙 4: 모델은 틀린다고 가정한다).

엔진 안쪽은 영어다 (M2.5). 안전망은 영어 글만 본다: 모델이 영어로 쓰지 않으면 루프가 한 번
영어로 다시 쓰게 하고, 그래도 아니면 번역기로 영어로 옮긴 뒤 여기에 넘긴다 (영어로 통일해 비교).
그래서 낱말 목록이 영어 한 벌이면 된다.

작은 모델(qwen3.5:4b)이 실제로 낸 실수를 막는다.
- "조건을 넓혀 보겠습니다"라고 해 놓고 아무것도 안 하고 끝냄 (promise_guard)
- 파일에 관한 답을 도구로 확인하지 않고 지어냄 (about_files + 루프의 근거 확인)
- 최신 정보(뉴스, 가격, 최근 논문)를 웹에서 확인하지 않고 기억으로 답함 (needs_evidence, M3)

안전망 하나는 한 작업에서 한 번만 되돌린다. 두 번째 답은 그대로 받는다 (무한히 돌지 않게).
되돌린 답은 화면에 나가지 않고 기억에도 남지 않는다 (loop.py).
"""

from __future__ import annotations

import re
from typing import Callable

from glados.core.context import CHECK_MARK

# (영어 글, 도구가 있는지) -> (화면 글 키, 모델에게 줄 안내) 또는 None
Guard = Callable[[str, bool], "tuple[str, str] | None"]

NUDGE_PROMISE = (f"{CHECK_MARK} Your answer only says what you are going to do. Do it now with a tool call, "
                 "or state plainly that it cannot be done.")

# "I'll try a broader search", "Let me check", "I will relax the conditions" ...
PROMISE = re.compile(
    r"\bI(?:'ll| will| shall| am going to|'m going to)\s+(?:now\s+|then\s+)?"
    r"(?:try|search|look|check|broaden|widen|relax|loosen|find|read|run|retry|attempt)\b"
    r"|\blet me\s+(?:try|search|look|check|find|read|run)\b"
    r"|\b(?:trying|searching|looking)\s+again\b",
    re.IGNORECASE,
)

# 파일을 다루는 이야기인가 (확인하지 않은 답을 한 번 되돌리는 데 씀)
FILE_TALK = re.compile(
    r"\b(?:files?|folders?|director(?:y|ies)|documents?|docs?|papers?|thesis|dissertations?|contracts?|pdfs?|docx"
    r"|downloads?|desktop)\b"
    r"|\b(?:find\w*|found|search\w*|look(?:ing|ed)?\s+for|read(?:s|ing)?|summar\w*|sav(?:e|ed|ing)|edit\w*|renam\w*)\b"
    r"|\.(?:pdf|docx?|md|txt|csv|json|py|hwp|xlsx|pptx)\b",
    re.IGNORECASE,
)


# 때가 지나면 바뀌는 정보인가 (웹에서 확인해야 하는데 기억으로 지어내기 쉬운 것)
FRESH_TALK = re.compile(
    r"\b(?:latest|newest|recent(?:ly)?|news|today|tonight|yesterday|this (?:week|month|year)|prices?|weather"
    r"|released?|web|online|internet|websites?|urls?|links?|look\s+up)\b"
    r"|https?://",
    re.IGNORECASE,
)


def about_files(text: str) -> bool:
    """글(연구원님 요청 + 모델의 영어 생각과 답)이 파일을 다루는가."""
    return bool(FILE_TALK.search(text))


def needs_evidence(text: str) -> bool:
    """확인하고 답해야 하는 이야기인가: 파일, 또는 때가 지나면 바뀌는 정보 (M3: 웹으로 확인)."""
    return bool(FILE_TALK.search(text) or FRESH_TALK.search(text))


def promise_guard(english: str, has_tools: bool) -> tuple[str, str] | None:
    """하겠다고만 하고 끝낸 답. 도구가 있을 때만 (없으면 할 수 있는 게 없다)."""
    if has_tools and PROMISE.search(english):
        return "guard.promise", NUDGE_PROMISE
    return None


DEFAULT_GUARDS: tuple[Guard, ...] = (promise_guard,)
