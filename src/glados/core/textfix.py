"""깨진 글자 고치기 (M6.2).

Windows 파일 이름과 일부 PDF에서 뽑은 글에는 짝 없는 서로게이트(\\ud800-\\udfff)가 섞일 수 있다
(이모지를 반으로 자른 이름, 글꼴 표가 깨진 PDF). 파이썬 문자열로는 괜찮지만 UTF-8로 쓰는 순간
(SQLite 캐시·기록, JSONL, 모델에게 보내는 JSON) UnicodeEncodeError가 나서 찾기 전체가 멈췄다.
글이 밖으로 나가는 곳(문서 글을 뽑을 때, 도구 결과, 모델 호출)에서 U+FFFD(�)로 바꾼다.
"""

from __future__ import annotations

import re
from typing import Any

SURROGATE = re.compile(r"[\ud800-\udfff]")


def clean(text: str) -> str:
    """짝 없는 서로게이트를 �로. 파이썬 str 안의 서로게이트는 늘 짝이 없다 (짝이면 한 글자로 들어 있음)."""
    return SURROGATE.sub("�", text) if SURROGATE.search(text) else text


def clean_any(value: Any) -> Any:
    """문자열·목록·사전 안의 글을 모두."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [clean_any(v) for v in value]
    if isinstance(value, tuple):
        return tuple(clean_any(v) for v in value)
    if isinstance(value, dict):
        return {clean_any(k): clean_any(v) for k, v in value.items()}
    return value
