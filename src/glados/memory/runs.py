"""서버 실행 기록 (M5): GLADOS_HOME/runs.db.

실행 하나 = EXP-NN 하나. 이 컴퓨터가 기억하는 것(서버, 명령, GPU, tmux 세션, 커밋, 상태, 시각)이고,
서버 쪽에는 <작업 폴더>/.glados/runs/EXP-NN/ 에 명령·실행 스크립트·출력(output.log)·커밋·diff·종료 코드가 남는다.
GLaDOS를 다시 켜면 이 기록으로 서버를 다시 보고 꺼진 동안 끝난 실행을 알린다.
여러 스레드(작업, 배경 감시)에서 쓴다: 잠금 하나로 차례대로.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ACTIVE = ("waiting", "running")
ENDED = ("done", "failed", "lost", "stopped", "cancelled")
ID_RE = re.compile(r"^EXP-(\d+)$")


@dataclass
class Run:
    id: str                                  # EXP-03
    server: str
    command: str
    cwd: str = ""                            # 서버의 실행 폴더 (절대 경로)
    dir: str = ""                            # 서버의 실행 기록 폴더 <작업 폴더>/.glados/runs/EXP-03
    session: str = ""                        # tmux 세션 이름
    want: int = 1                            # GPU 몇 장
    pick: list[int] = field(default_factory=list)    # 연구원님이 고른 GPU (비면 빈 것 중에서)
    gpus: list[int] = field(default_factory=list)    # 실제로 쓴 GPU
    state: str = "waiting"                   # waiting | running | done | failed | lost | stopped | cancelled
    created: float = 0.0
    started: float = 0.0
    ended: float = 0.0
    exit_code: int | None = None
    commit: str = ""
    branch: str = ""
    dirty: int = 0                           # 커밋 안 된 파일 수 (diff.patch로 저장)
    git: bool = False
    session_id: str = ""                     # 만든 GLaDOS 세션 (대기 줄은 그 세션 안에서만 기다린다)
    task_id: str = ""
    alerts: list[str] = field(default_factory=list)  # 이미 보낸 알림 (nan, time ...)
    note: str = ""                           # 마지막으로 본 출력 한 줄 / 기다리는 까닭 (영어)
    seen: float = 0.0                        # 마지막으로 서버에서 확인한 시각
    # M5.1 메모리: GPU마다 필요한 메모리(0이면 모름 -> 빈 GPU만), 어떻게 정했는지, 실제로 쓴 최대
    need_mb: float = 0.0
    need_how: str = ""                       # said | measured | estimate | "" (모름)
    need_why: str = ""                       # 근거 (추정이면 모델·정밀도·배치..., 잰 값이면 그 실행 번호)
    peak_mb: float = 0.0                     # 실행 중 내 프로세스가 GPU 하나에서 쓴 최대 메모리
    # M5.2·5.3 실험 세션: 프로젝트, 지표를 뽑는 방법, 로그를 어디까지 가져왔는지, 이 컴퓨터의 실험 폴더
    project: str = ""
    metric_spec: dict = field(default_factory=dict)
    log_offset: int = 0
    metrics_at: float = 0.0
    exp_dir: str = ""
    best: dict = field(default_factory=dict)   # 지표마다 {"value", "x"} (최고), 그리고 "_rows" (표의 줄 수)
    # 지표 뽑기 상태: 지금 x, 이름 목록과 예시, 패턴을 정할 때 알던 이름, 출력 끝 50줄, 줄 중간에서 끊긴 글 ...
    metric_state: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def number(self) -> int:
        m = ID_RE.match(self.id)
        return int(m.group(1)) if m else 0


def run_id(n: int) -> str:
    return f"EXP-{n:02d}"


def id_number(text: str) -> int:
    m = ID_RE.match(text.strip().upper())
    return int(m.group(1)) if m else 0


class RunStore:
    def __init__(self, home: Path):
        self._lock = threading.RLock()
        home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(home / "runs.db"), check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, server TEXT, state TEXT, created REAL, data TEXT
            );
            """
        )
        self.db.commit()

    @staticmethod
    def _load(data: str) -> Run:
        raw = json.loads(data)
        names = {f.name for f in fields(Run)}
        return Run(**{k: v for k, v in raw.items() if k in names})

    def save(self, run: Run) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO runs (id, server, state, created, data) VALUES (?, ?, ?, ?, ?)",
                            (run.id, run.server, run.state, run.created, json.dumps(asdict(run), ensure_ascii=False)))
            self.db.commit()

    def get(self, rid: str) -> Run | None:
        key = run_id(id_number(rid)) if id_number(rid) else rid.strip()
        with self._lock:
            row = self.db.execute("SELECT data FROM runs WHERE id = ?", (key,)).fetchone()
        return self._load(row[0]) if row else None

    def all(self, limit: int = 200) -> list[Run]:
        """최근 것부터."""
        with self._lock:
            rows = self.db.execute("SELECT data FROM runs ORDER BY created DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._load(r[0]) for r in rows]

    def active(self) -> list[Run]:
        """기다리거나 도는 실행, 오래된 것부터 (대기 줄 순서)."""
        with self._lock:
            rows = self.db.execute("SELECT data FROM runs WHERE state IN (?, ?) ORDER BY created, id",
                                   ACTIVE).fetchall()
        return [self._load(r[0]) for r in rows]

    def on_server(self, server: str) -> list[Run]:
        with self._lock:
            rows = self.db.execute("SELECT data FROM runs WHERE server = ? ORDER BY created, id", (server,)).fetchall()
        return [self._load(r[0]) for r in rows]

    def next_id(self, taken: set[str] | None = None) -> str:
        """이 컴퓨터의 기록과 서버에 이미 있는 실행 폴더(taken) 다음 번호."""
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM runs").fetchall()]
        top = max([id_number(i) for i in ids] + [id_number(i) for i in (taken or set())] + [0])
        return run_id(top + 1)

    def close(self) -> None:
        with self._lock:
            self.db.close()
