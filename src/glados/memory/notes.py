"""오래 기억할 것 (M6): GLADOS_HOME/MEMORY.md.

연구원님이 "기억해"라고 한 것만 한 줄씩 남는다 (memory.note, 매번 승인). 작업마다 시스템 프롬프트에 넣는다
(맥락 길이에 맞게 최근 것부터). 사람이 읽고 고칠 수 있는 글 파일이다: 줄 하나가 메모 하나.
GLaDOS는 "- 날짜 · 글"로 쓰고, 연구원님이 손으로 쓴 줄은 "- " 없이도 메모로 친다 (M6.2). 제목(#)·빈 줄·
<!-- 주석 -->은 메모가 아니다. /memory 로 보고, /memory del N 으로 지운다. 파일을 직접 고쳐도 된다.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

from glados.safety.privacy import secret_in_note

FILE = "MEMORY.md"
NOTE_MAX = 300           # 메모 한 줄의 최대 길이
HEADER = {
    "ko": "# GLaDOS 기억\n\n<!-- 연구원님이 기억하라고 한 것. 줄 하나가 메모 하나입니다. 고치거나 지우거나 직접 써도 됩니다. -->\n\n",
    "en": "# GLaDOS memory\n\n<!-- Things you asked GLaDOS to remember. One note per line. Edit, delete, or add your own. -->\n\n",
}
_LOCK = threading.Lock()
LIST_MARK = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def budget(num_ctx: int) -> int:
    """시스템 프롬프트에 넣을 메모 글자 수 (16k에서는 지난 대화 몫을 지키려고 짧게)."""
    if num_ctx <= 16384:
        return 500
    return 1500 if num_ctx <= 32768 else 3000


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", " ".join(text.split())).casefold()


def clean(text: str) -> str:
    """한 줄로, 앞의 목록 기호를 빼고, 길면 자른다."""
    line = " ".join(str(text).split()).lstrip("-*# ").strip()
    return line if len(line) <= NOTE_MAX else line[:NOTE_MAX - 1] + "…"


def _body(item: str) -> str:
    """'2026-11-11 · 글' -> '글' (날짜가 없으면 그대로)."""
    head, sep, rest = item.partition(" · ")
    return rest if sep and len(head) == 10 and head[4] == "-" else item


class Notes:
    def __init__(self, home: Path, lang: str = "ko"):
        self.path = home / FILE
        self.lang = lang if lang in HEADER else "en"

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

    def _notes(self) -> list[tuple[int, str]]:
        """(줄 번호, 메모). 제목·빈 줄·주석(여러 줄도)은 빼고, 앞의 목록 기호(-, *, +, 1.)는 뗀다."""
        out: list[tuple[int, str]] = []
        in_comment = False
        for i, raw in enumerate(self._lines()):
            ln = raw.lstrip("\ufeff").strip()
            if in_comment:
                in_comment = "-->" not in ln
                continue
            if ln.startswith("<!--"):
                in_comment = "-->" not in ln
                continue
            if not ln or ln.startswith("#"):
                continue
            text = LIST_MARK.sub("", ln).strip()
            if text:
                out.append((i, text))
        return out

    def items(self) -> list[str]:
        return [text for _, text in self._notes()]

    def add(self, text: str, today: datetime | None = None) -> tuple[int, str, bool]:
        """(번호, 저장한 줄, 새로 넣었는지). 같은 글이 이미 있으면 넣지 않는다."""
        line = clean(text)
        if not line:
            raise ValueError("empty note")
        with _LOCK:
            items = self.items()
            for i, item in enumerate(items, 1):
                if _fold(_body(item)) == _fold(line):
                    return i, item, False
            item = f"{(today or datetime.now()):%Y-%m-%d} · {line}"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists() or not self._lines():
                self.path.write_text(HEADER[self.lang], encoding="utf-8", newline="\n")
            text_now = self.path.read_text(encoding="utf-8")
            sep = "" if text_now.endswith("\n") else "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(f"{sep}- {item}\n")
            return len(items) + 1, item, True

    def remove(self, n: int) -> str | None:
        """n번째 메모를 지운다 (1부터). 지운 글, 없으면 None. 다른 줄(머리말, 연구원님이 쓴 글)은 그대로 둔다."""
        with _LOCK:
            notes = self._notes()
            if not 1 <= n <= len(notes):
                return None
            index, text = notes[n - 1]
            lines = self._lines()
            del lines[index]
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            return text

    def prompt(self, limit: int) -> str:
        """시스템 프롬프트에 넣을 메모 (limit 글자 안에서 최근 것부터 담고, 순서는 오래된 것부터)."""
        kept: list[str] = []
        used = 0
        items = [item for item in self.items() if not secret_in_note(item)]   # 비밀로 보이면 모델에게 주지 않음 (M6.2)
        for item in reversed(items):
            line = f"- {item}"
            if used + len(line) + 1 > limit:
                break
            kept.append(line)
            used += len(line) + 1
        kept.reverse()
        if len(kept) < len(items):
            kept.insert(0, f"({len(items) - len(kept)} older notes left out)")
        return "\n".join(kept)
