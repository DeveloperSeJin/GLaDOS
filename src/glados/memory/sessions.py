"""대화 세션 (M6): GLADOS_HOME/sessions.db.

세션 하나 = 대화 하나 (GLaDOS를 켜거나 /new 한 뒤부터). 작업이 끝날 때마다 그 턴(연구원님 말, 걸음, 도구 결과,
답, 작업 메모)을 저장한다. 다시 켜면 /resume 이나 말("어제 하던 거 이어서")로 지난 세션을 불러와 이어 간다.
첫 작업이 끝나야 세션이 생긴다 (빈 세션은 남기지 않음).
'!명령'만 있는 세션은 목록·이어하기에서 뺀다 (M6.6: "!git status"가 지난 대화로 뜨던 문제). 제목은 첫 요청
('!명령' 말고). 저장은 그대로 한다.

이 컴퓨터에만 남는다. 기억 속 글은 영어(엔진 안쪽), 연구원님 말만 쓴 그대로다 (core/context.py).
여러 스레드(TUI 작업 스레드, 화면)에서 쓴다: 잠금 하나로 차례대로.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from glados.core.context import Turn

TITLE_MAX = 80          # 세션 제목 (첫 요청)
LIST_MAX = 10           # /resume 목록
WORD = re.compile(r"[\w.\-]{2,}", re.UNICODE)


@dataclass
class SessionInfo:
    id: str
    started: float
    last: float
    title: str           # 첫 요청 (연구원님이 쓴 그대로, 줄임)
    count: int           # 턴 수

    def when(self) -> str:
        return datetime.fromtimestamp(self.last).strftime("%m-%d %H:%M")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _title(request: str) -> str:
    line = " ".join(request.split())
    return line if len(line) <= TITLE_MAX else line[:TITLE_MAX - 1] + "…"


# 세션 행. 제목이 '!명령'이면 '!명령'이 아닌 첫 요청으로 (없으면 NULL: 명령만 있는 세션)
_ROW = ("SELECT s.id, s.started, s.last, CASE WHEN substr(s.title, 1, 1) != '!' THEN s.title ELSE "
        "(SELECT t.request FROM turns t WHERE t.session_id = s.id AND substr(t.request, 1, 1) != '!' "
        "ORDER BY t.idx LIMIT 1) END AS talk, s.count FROM sessions s")


def _turn(data: str) -> Turn:
    raw = json.loads(data)
    names = {f.name for f in fields(Turn)}
    return Turn(**{k: v for k, v in raw.items() if k in names})


class SessionStore:
    def __init__(self, home: Path):
        self._lock = threading.RLock()
        home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(home / "sessions.db"), check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started REAL, last REAL, title TEXT, count INTEGER
            );
            CREATE TABLE IF NOT EXISTS turns (
                session_id TEXT, idx INTEGER, ts REAL, request TEXT, data TEXT,
                PRIMARY KEY (session_id, idx)
            );
            """
        )
        self.db.commit()

    # ------------------------------------------------------------------
    def new(self, title: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sid = "S" + datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        with self._lock:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (sid, now, now, _title(title), 0))
            self.db.commit()
        return sid

    def append(self, sid: str, turn: Turn, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            row = self.db.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM turns WHERE session_id = ?", (sid,)).fetchone()
            idx = int(row[0])
            self.db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?)",
                            (sid, idx, now, turn.request, json.dumps(asdict(turn), ensure_ascii=False)))
            self.db.execute("UPDATE sessions SET last = ?, count = ? WHERE id = ?", (now, idx + 1, sid))
            self.db.commit()

    # ------------------------------------------------------------------
    @staticmethod
    def _info(row: tuple) -> SessionInfo:
        return SessionInfo(row[0], row[1], row[2], _title(row[3] or ""), int(row[4] or 0))

    def get(self, sid: str) -> SessionInfo | None:
        with self._lock:
            row = self.db.execute(f"SELECT * FROM ({_ROW}) WHERE id = ?", (sid,)).fetchone()
            if row is not None and row[3] is None:      # 명령만 있는 세션: 저장된 제목 그대로
                title = self.db.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
                row = (*row[:3], title, row[4])
        return self._info(row) if row else None

    def recent(self, n: int = LIST_MAX, exclude: str = "") -> list[SessionInfo]:
        """최근에 쓴 것부터 (지금 세션, 빈 세션, '!명령'만 있는 세션은 빼고)."""
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM ({_ROW}) WHERE id != ? AND count > 0 AND talk IS NOT NULL "
                "ORDER BY last DESC, id DESC LIMIT ?", (exclude, n)).fetchall()
        return [self._info(r) for r in rows]

    def turns(self, sid: str) -> list[Turn]:
        with self._lock:
            rows = self.db.execute("SELECT data FROM turns WHERE session_id = ? ORDER BY idx", (sid,)).fetchall()
        return [_turn(r[0]) for r in rows]

    def find(self, query: str, exclude: str = "", scan: int = 50) -> SessionInfo | None:
        """query의 낱말이 가장 많이 나오는 세션 (연구원님 말과 답에서). 맞은 낱말 수가 같으면 작업이 많은 쪽,
        그것도 같으면 최근 것 (M6.1: "PDF"만 넘기면 방금 실패한 "이어서 하자" 시도(작업 1개)를 고르던 문제).
        query가 비면 가장 최근 세션. 아무 낱말도 안 맞으면 None."""
        sessions = self.recent(scan, exclude)
        if not sessions:
            return None
        words = {_fold(w) for w in WORD.findall(query or "")}
        if not words:
            return sessions[0]
        best: tuple[int, SessionInfo] | None = None
        for info in sessions:
            text = _fold(info.title + "\n" + "\n".join(t.request + "\n" + t.answer for t in self.turns(info.id)))
            score = sum(1 for w in words if w in text)
            if score and (best is None or (score, info.count) > (best[0], best[1].count)):   # 최근 것부터 보므로 완전 동점이면 최근 것
                best = (score, info)
        return best[1] if best else None

    def close(self) -> None:
        with self._lock:
            self.db.close()


# ---------------------------------------------------------------------- 모델에게 보여 줄 요약
def recap(info: SessionInfo, turns: list[Turn], last: int = 8, width: int = 160) -> str:
    """불러온 세션의 요약 (영어, 모델용): 언제, 몇 개, 최근 작업 (요청 -> 답 첫 줄)."""
    when = datetime.fromtimestamp(info.last).strftime("%Y-%m-%d %H:%M")
    lines = [f"Conversation from {when} ({info.count} tasks), first request: {info.title}"]
    shown = turns[-last:]
    if len(turns) > len(shown):
        lines.append(f"(the {len(turns) - len(shown)} earlier tasks are left out here)")
    for i, turn in enumerate(shown, len(turns) - len(shown) + 1):
        said = " ".join(turn.request.split())
        answer = next((ln for ln in (turn.answer or turn.stop_note or "(no answer)").splitlines() if ln.strip()), "")
        lines.append(f"{i}. {said[:width]} -> {answer[:width]}")
    return "\n".join(lines)
