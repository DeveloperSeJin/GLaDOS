"""작업 기록(SQLite + JSONL)과 정책 판정 기록(logs/policy.jsonl).

- tasks.db: 작업과 이벤트. 나중에 이어하기·검색에 쓴다
- logs/tasks.jsonl: 같은 내용을 한 줄씩 (사람이 읽거나 다른 도구로 옮기기 쉽게)
- logs/policy.jsonl: 도구, 인자 요약, 판정, 적용 규칙, 시각, 결정한 사람
파일 내용 같은 긴 값은 앞부분만 남긴다.
TUI는 작업을 스레드에서 돌리므로 기록은 여러 스레드에서 쓴다 (잠금 하나로 차례대로).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from glados.core.events import Event

ARG_LIMIT = 200
_LOCK = threading.RLock()     # 기록 파일·DB는 한 번에 한 스레드만


def summarize_args(args: dict[str, Any], limit: int = ARG_LIMIT) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        out[k] = text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:   # 세 OS 모두 같은 줄바꿈
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TaskLog:
    def __init__(self, home: Path):
        self.home = home
        self.jsonl = home / "logs" / "tasks.jsonl"
        self.db = sqlite3.connect(str(home / "tasks.db"), check_same_thread=False)   # 잠금으로 차례대로
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, created REAL, request TEXT, status TEXT, finished REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                task_id TEXT, ts REAL, kind TEXT, data TEXT
            );
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
            """
        )
        self.db.commit()

    def new_task(self, request: str) -> str:
        task_id = "T" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        now = time.time()
        with _LOCK:
            self.db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", (task_id, now, request, "running", None))
            self.db.commit()
        _append_jsonl(self.jsonl, {"task": task_id, "ts": now, "kind": "task", "request": request})
        return task_id

    def event(self, task_id: str, ev: Event) -> None:
        data = json.dumps(ev.data, ensure_ascii=False)
        with _LOCK:
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (task_id, ev.ts, ev.kind, data))
            self.db.commit()
        _append_jsonl(self.jsonl, {"task": task_id, "ts": ev.ts, "kind": ev.kind, **ev.data})

    def finish(self, task_id: str, status: str) -> None:
        with _LOCK:
            self.db.execute("UPDATE tasks SET status = ?, finished = ? WHERE id = ?", (status, time.time(), task_id))
            self.db.commit()

    def recent(self, n: int = 10) -> list[tuple[str, str, str]]:
        with _LOCK:
            rows = self.db.execute(
                "SELECT id, request, status FROM tasks ORDER BY created DESC LIMIT ?", (n,)
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def close(self) -> None:
        with _LOCK:
            self.db.close()


class PolicyLog:
    def __init__(self, home: Path):
        self.path = home / "logs" / "policy.jsonl"

    def write(
        self,
        tool: str,
        args: dict[str, Any],
        verdict: str,
        rule: str,
        decided_by: str,
        task_id: str = "",
    ) -> None:
        _append_jsonl(
            self.path,
            {
                "ts": time.time(),
                "task": task_id,
                "tool": tool,
                "args": summarize_args(args),
                "verdict": verdict,
                "rule": rule,
                "decided_by": decided_by,
            },
        )

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
