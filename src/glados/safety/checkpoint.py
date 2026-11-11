"""체크포인트: fs.edit로 고치기 전에 원본을 남기고, /undo로 되돌린다.

GLADOS_HOME/checkpoints/ 아래에 둔다. 이 폴더는 GLaDOS 도구로는 고칠 수 없는 안전장치 위치이고,
엔진 코드만 직접 쓴다. 기록은 index.jsonl (utf-8, 줄바꿈 \\n).
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Checkpoint:
    id: str
    original: str     # 고친 파일
    saved: str        # 고치기 전 사본
    task: str
    ts: float


class Checkpoints:
    def __init__(self, home: Path):
        self.dir = home / "checkpoints"
        self.index = self.dir / "index.jsonl"

    def _append(self, record: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.index.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _records(self) -> list[dict]:
        if not self.index.exists():
            return []
        out = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def save(self, original: Path, task_id: str = "") -> Checkpoint:
        """원본을 복사해 둔다."""
        self.dir.mkdir(parents=True, exist_ok=True)
        cp_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        saved = self.dir / f"{cp_id}-{original.name}"
        shutil.copy2(original, saved)
        cp = Checkpoint(cp_id, str(original), str(saved), task_id, time.time())
        self._append({"event": "save", **asdict(cp)})
        return cp

    def last(self) -> Checkpoint | None:
        """아직 되돌리지 않은 가장 최근 체크포인트."""
        records = self._records()
        undone = {r["id"] for r in records if r.get("event") == "undo"}
        for r in reversed(records):
            if r.get("event") == "save" and r["id"] not in undone:
                return Checkpoint(r["id"], r["original"], r["saved"], r.get("task", ""), r["ts"])
        return None

    def restore(self, cp: Checkpoint) -> Path:
        """원본을 체크포인트로 되돌린다. 되돌리기 직전 내용도 사본으로 남긴다."""
        original = Path(cp.original)
        if original.exists():
            shutil.copy2(original, self.dir / f"{cp.id}-undone-{original.name}")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cp.saved, original)
        self._append({"event": "undo", "id": cp.id, "ts": time.time()})
        return original
