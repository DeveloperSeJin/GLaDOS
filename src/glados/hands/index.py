"""내용 색인 (M7.1): 자주 찾는 폴더의 문서 글을 미리 읽어 SQLite FTS5(trigram)에 둔다.

- 어디: GLADOS_HOME/cache/index.sqlite. 이 컴퓨터 밖으로 나가지 않는다. 지워도 된다 (다음에 다시 읽음)
- 무엇: folders.toml search_default의 폴더(다운로드·문서·바탕 화면)에 있는 pdf·docx·hwp·hwpx·글 파일의 글 전체
  (파일마다 40만 자까지). 안전장치·보호 위치·숨은 폴더·클라우드 전용 파일은 읽지 않는다 (찾기와 같은 규칙).
  pdf는 쪽 사이에 \\f를 넣어 둔다 (몇 쪽에서 나왔는지 말할 수 있게)
- 언제: GLaDOS가 켜져 있는 동안 따로 낮은 우선순위 프로세스가 (`python -m glados.hands.index <GLADOS_HOME>`).
  최근 파일부터, 바뀐 파일만 다시 읽는다. 10분마다 다시 훑는다. GLaDOS가 꺼지면 같이 끝난다 (표준 입력이 닫힘).
  GLaDOS를 둘 켜도 색인은 하나만 돈다 (meta의 심장 박동)
- 찾기: fs.search content=true가 먼저 색인에서 찾고, 색인에 없거나 그 뒤에 바뀐 파일만 그 자리에서 읽는다.
  trigram은 세 글자 이상만 색인으로 찾는다. 두 글자 낱말(논문, 모델)은 글에서 직접 찾는다 (LIKE, 조금 느림)
- 뜻으로 찾기 (M7.2): 글을 읽은 파일마다 몇 군데(첫머리 + 고르게 8곳까지, 1000자씩)를 임베딩 모델
  (기본 qwen3-embedding:0.6b, Ollama)로 벡터로 바꿔 둔다. fs.search content=true가 낱말로 찾은 뒤, 질문과 뜻이
  가까운 파일을 따로 몇 개 보여 준다 (낱말이 달라도: "위약금" ↔ penalty clause). GLaDOS가 작업 중이면
  (index.busy) 벡터 만들기를 쉰다 (같은 GPU를 쓰므로). 모델이 없으면 낱말 색인만 한다
- 무엇이 무슨 문서인지 같은 판단은 하지 않는다 (글을 모아 둘 뿐)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

INDEX_FILE = "index.sqlite"      # GLADOS_HOME/cache 안
JOB_FILE = "index-job.json"      # GLaDOS가 색인 프로세스에 넘기는 것 (폴더, 위치 규칙)
BUSY_FILE = "index.busy"         # 작업 중이면 있다 (색인이 모델·GPU를 쓰는 일을 미룸, M7.2)
VERSION = 1                      # 글을 뽑는 방법이 바뀌면 올린다 (그 파일들을 다시 읽음)
FILE_CHARS = 400_000             # 한 파일에서 색인할 글 (긴 책은 앞부분)
FILE_BUDGET_S = 30.0             # 한 파일에 쓰는 시간 (넘으면 읽은 쪽까지)
TEXT_MAX_BYTES = 2_000_000       # 글 파일은 이보다 크면 색인하지 않는다 (로그 등)
MAX_FILES = 20_000               # 한 번 훑을 때 볼 파일 수 한도
RESCAN_S = 600.0                 # 다시 훑는 간격
BEAT_S = 20.0                    # 색인 프로세스가 살아 있다고 적는 간격
STALE_S = 90.0                   # 이보다 오래 소식이 없으면 그 색인 프로세스는 없는 것
MIN_TRIGRAM = 3                  # trigram 색인으로 찾을 수 있는 가장 짧은 낱말
PAGE = "\f"
DOC_TYPES = ("pdf", "docx", "hwp", "hwpx")
CHUNK_CHARS = 1000               # 뜻으로 찾기: 한 조각 길이
MAX_CHUNKS = 8                   # 파일마다 조각 수 (첫머리 + 고르게)
BUSY_WAIT_S = 5.0                # 작업 중이면 이만큼 쉬고 다시 본다

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE, path TEXT, size INTEGER, mtime INTEGER, version INTEGER,
    status TEXT, pages INTEGER, read_pages INTEGER, title TEXT, chars INTEGER, indexed REAL)""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS texts USING fts5(body, tokenize='trigram')",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS vecs (file_id INTEGER, n INTEGER, start INTEGER, vec BLOB, PRIMARY KEY (file_id, n))",
)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def file_key(path: str | Path) -> str:
    return os.path.normcase(str(path))


def fts5_ok() -> bool:
    """이 Python의 SQLite에 FTS5와 trigram이 있나 (없으면 색인 없이 예전처럼 그 자리에서 읽는다)."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def index_path(home: Path) -> Path:
    return home / "cache" / INDEX_FILE


@dataclass
class Entry:
    id: int
    key: str
    path: str
    size: int
    mtime: int
    version: int
    status: str
    pages: int
    read_pages: int
    title: str

    def fresh(self, st: os.stat_result) -> bool:
        """색인한 뒤로 파일이 그대로인가."""
        return self.version == VERSION and self.size == st.st_size and self.mtime == st.st_mtime_ns


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class FileIndex:
    """색인 파일 하나. 쓰는 쪽(색인 프로세스)과 읽는 쪽(GLaDOS)이 같이 열어도 되게 WAL로."""

    def __init__(self, home: Path):
        path = index_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(path), timeout=15, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        for statement in SCHEMA:
            self.con.execute(statement)
        columns = {r[1] for r in self.con.execute("PRAGMA table_info(files)").fetchall()}
        if "vec_model" not in columns:                 # M7.1에서 만든 색인: 벡터를 만든 모델 칸을 더한다
            self.con.execute("ALTER TABLE files ADD COLUMN vec_model TEXT DEFAULT ''")

    def close(self) -> None:
        with self._lock:
            try:
                self.con.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ 메타 (상태, 심장 박동)
    def meta(self, k: str, default: str = "") -> str:
        with self._lock:
            row = self.con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, **items: Any) -> None:
        with self._lock:
            self.con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in items.items()])

    def claim(self, pid: int, now: float | None = None) -> bool:
        """색인 프로세스는 하나만: 다른 것이 최근에 살아 있다고 적었으면 False."""
        now = time.time() if now is None else now
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                rows = dict(self.con.execute("SELECT k, v FROM meta WHERE k IN ('pid', 'beat')").fetchall())
                other = rows.get("pid", "") not in ("", str(pid))
                if other and now - float(rows.get("beat") or 0) < STALE_S:
                    self.con.execute("ROLLBACK")
                    return False
                self.con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("pid", str(pid)), ("beat", str(now))])
                self.con.execute("COMMIT")
                return True
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    def running(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        try:
            return now - float(self.meta("beat") or 0) < STALE_S
        except ValueError:
            return False

    # ------------------------------------------------------------------ 파일
    def entries(self, keys: list[str]) -> dict[str, Entry]:
        out: dict[str, Entry] = {}
        with self._lock:
            for i in range(0, len(keys), 400):
                part = keys[i:i + 400]
                rows = self.con.execute(
                    "SELECT id, key, path, size, mtime, version, status, pages, read_pages, title FROM files "
                    f"WHERE key IN ({', '.join('?' * len(part))})", part).fetchall()
                for r in rows:
                    out[r[1]] = Entry(*r)
        return out

    def all_ids(self) -> dict[str, int]:
        with self._lock:
            return dict(self.con.execute("SELECT key, id FROM files").fetchall())

    def put(self, path: Path, st: os.stat_result, status: str, pages: int = 0, read_pages: int = 0,
            title: str = "", text: str = "") -> int:
        key = file_key(path)
        text = nfc(text)
        values = (str(path), st.st_size, st.st_mtime_ns, VERSION, status, pages, read_pages, nfc(title), len(text),
                  time.time())
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                row = self.con.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
                if row:
                    fid = int(row[0])
                    self.con.execute("UPDATE files SET path = ?, size = ?, mtime = ?, version = ?, status = ?, pages = ?, "
                                     "read_pages = ?, title = ?, chars = ?, indexed = ? WHERE id = ?", (*values, fid))
                    self.con.execute("DELETE FROM texts WHERE rowid = ?", (fid,))
                    self.con.execute("DELETE FROM vecs WHERE file_id = ?", (fid,))
                    self.con.execute("UPDATE files SET vec_model = '' WHERE id = ?", (fid,))
                else:
                    fid = int(self.con.execute(
                        "INSERT INTO files (path, size, mtime, version, status, pages, read_pages, title, chars, indexed, key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, key)).lastrowid)
                if text:
                    self.con.execute("INSERT INTO texts (rowid, body) VALUES (?, ?)", (fid, text))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return fid

    def remove(self, ids: list[int]) -> None:
        with self._lock:
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                marks = ", ".join("?" * len(part))
                self.con.execute("BEGIN IMMEDIATE")
                try:
                    self.con.execute(f"DELETE FROM texts WHERE rowid IN ({marks})", part)
                    self.con.execute(f"DELETE FROM vecs WHERE file_id IN ({marks})", part)
                    self.con.execute(f"DELETE FROM files WHERE id IN ({marks})", part)
                    self.con.execute("COMMIT")
                except BaseException:
                    self.con.execute("ROLLBACK")
                    raise

    # ------------------------------------------------------------------ 찾기
    def match(self, groups: list[list[str]], limit: int = 5000) -> list[int]:
        """낱말 묶음이 모두 들어 있는 글의 파일 id. 묶음 안은 '또는'(낱말과 그 영어 뜻), 묶음끼리는 '그리고'.
        세 글자 이상은 trigram 색인으로, 짧은 낱말이 낀 묶음은 글에서 직접 (LIKE)."""
        fts: list[str] = []
        likes: list[str] = []
        params: list[str] = []
        for group in groups:
            words = list(dict.fromkeys(nfc(w).strip() for w in group if w and w.strip()))
            if not words:
                continue
            if all(len(w) >= MIN_TRIGRAM for w in words):
                fts.append("(" + " OR ".join('"' + w.replace('"', '""') + '"' for w in words) + ")")
            else:
                likes.append("(" + " OR ".join("body LIKE ? ESCAPE '\\'" for _ in words) + ")")
                params += ["%" + _like(w) + "%" for w in words]
        if not fts and not likes:
            return []
        where: list[str] = []
        args: list[Any] = []
        if fts:
            where.append("texts MATCH ?")
            args.append(" AND ".join(fts))
        where += likes
        args += params
        with self._lock:
            rows = self.con.execute(f"SELECT rowid FROM texts WHERE {' AND '.join(where)} LIMIT ?",
                                    (*args, limit)).fetchall()
        return [int(r[0]) for r in rows]

    def snippet(self, fid: int, needles: list[str], width: int = 60) -> tuple[str, int]:
        """처음 맞은 자리 앞뒤 글(한 줄)과 쪽 번호 (pdf가 아니면 0)."""
        with self._lock:
            row = self.con.execute("SELECT body FROM texts WHERE rowid = ?", (fid,)).fetchone()
        body = str(row[0]) if row else ""
        for needle in needles:
            if not needle:
                continue
            m = re.search(re.escape(nfc(needle)), body, re.IGNORECASE)
            if m:
                page = body.count(PAGE, 0, m.start()) + 1 if PAGE in body else 0
                a, b = max(0, m.start() - width), min(len(body), m.end() + width)
                text = " ".join(body[a:b].split())
                return ("…" if a else "") + text + ("…" if b < len(body) else ""), page
        return "", 0

    # ------------------------------------------------------------------ 뜻으로 찾기 (M7.2)
    def body(self, fid: int) -> str:
        with self._lock:
            row = self.con.execute("SELECT body FROM texts WHERE rowid = ?", (fid,)).fetchone()
        return str(row[0]) if row else ""

    def needs_vectors(self, model: str, limit: int = 50) -> list[tuple[int, str]]:
        """벡터가 없거나 다른 모델로 만든 파일 (최근 것부터): [(id, 제목)]."""
        with self._lock:
            rows = self.con.execute("SELECT id, title FROM files WHERE status = 'ok' AND chars > 0 AND "
                                    "COALESCE(vec_model, '') != ? ORDER BY mtime DESC LIMIT ?", (model, limit)).fetchall()
        return [(int(r[0]), str(r[1] or "")) for r in rows]

    def put_vectors(self, fid: int, model: str, chunks: list[tuple[int, str]], vectors: list[list[float]]) -> None:
        import numpy as np

        rows = []
        for n, ((start, _), vec) in enumerate(zip(chunks, vectors)):
            v = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(v)) or 1.0
            rows.append((fid, n, start, (v / norm).astype(np.float16).tobytes()))
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self.con.execute("DELETE FROM vecs WHERE file_id = ?", (fid,))
                self.con.executemany("INSERT INTO vecs VALUES (?, ?, ?, ?)", rows)
                self.con.execute("UPDATE files SET vec_model = ? WHERE id = ?", (model, fid))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    def nearest(self, ids: list[int], query: list[float], model: str) -> list[tuple[int, float, int]]:
        """질문 벡터와 가장 가까운 조각: [(파일 id, 닮은 정도 -1~1, 조각 시작 위치)], 가까운 순. 파일마다 하나."""
        import numpy as np

        q = np.asarray(query, dtype=np.float32)
        q /= float(np.linalg.norm(q)) or 1.0
        best: dict[int, tuple[float, int]] = {}
        with self._lock:
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                rows = self.con.execute(
                    f"SELECT v.file_id, v.start, v.vec FROM vecs v JOIN files f ON f.id = v.file_id "
                    f"WHERE f.vec_model = ? AND v.file_id IN ({', '.join('?' * len(part))})", (model, *part)).fetchall()
                kept = [r for r in rows if len(r[2]) == q.size * 2]     # 길이가 다른 벡터(다른 설정)는 건너뛴다
                if not kept:
                    continue
                mat = np.frombuffer(b"".join(r[2] for r in kept), dtype=np.float16)
                scores = mat.reshape(len(kept), q.size).astype(np.float32) @ q
                for (fid, start, _), score in zip(kept, scores.tolist()):
                    if fid not in best or score > best[fid][0]:
                        best[fid] = (score, int(start))
        return sorted(((fid, sc, st) for fid, (sc, st) in best.items()), key=lambda x: -x[1])

    def vector_count(self, ids: list[int], model: str) -> int:
        """이 파일들 가운데 벡터가 준비된 것 수."""
        n = 0
        with self._lock:
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                n += self.con.execute(f"SELECT COUNT(*) FROM files WHERE vec_model = ? AND id IN "
                                      f"({', '.join('?' * len(part))})", (model, *part)).fetchone()[0]
        return int(n)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by = dict(self.con.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
            partial = self.con.execute(
                "SELECT COUNT(*) FROM files WHERE status = 'ok' AND read_pages < pages").fetchone()[0]
            vec = dict(self.con.execute("SELECT COALESCE(vec_model, ''), COUNT(*) FROM files WHERE status = 'ok' "
                                        "AND chars > 0 GROUP BY COALESCE(vec_model, '')").fetchall())
            meta = dict(self.con.execute("SELECT k, v FROM meta").fetchall())
        return {"by_status": by, "files": sum(by.values()), "partial": int(partial), "vectors": vec, **meta}


def open_index(home: Path) -> FileIndex | None:
    """찾기에서 쓸 색인 (없거나 못 열면 None: 예전처럼 그 자리에서 읽는다)."""
    if not index_path(home).exists() or not fts5_ok():
        return None
    try:
        return FileIndex(home)
    except (sqlite3.Error, OSError):
        return None


# ---------------------------------------------------------------------- 훑기 (색인 프로세스)
def walk(roots: list[str], places: Any, exts: set[str], limit: int = MAX_FILES) -> Iterator[tuple[Path, os.stat_result]]:
    """색인할 파일: 숨은 폴더·SKIP_DIRS·안전장치·보호 위치는 건너뛴다 (찾기와 같은 규칙)."""
    from glados.hands.fs import SKIP_DIRS
    from glados.safety.locations import classify

    seen: set[str] = set()
    count = 0
    for root in roots:
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                items = list(os.scandir(folder))
            except OSError:
                continue
            for item in items:
                name = item.name
                if name.startswith("."):
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        if name in SKIP_DIRS:
                            continue
                        loc = classify(item.path, places)
                        if not (loc.safety or loc.protected):
                            stack.append(item.path)
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
                if ext not in exts:
                    continue
                k = file_key(item.path)
                if k in seen:
                    continue
                seen.add(k)
                loc = classify(item.path, places)
                if loc.safety or loc.protected:
                    continue
                try:
                    st = item.stat()
                except OSError:
                    continue
                yield Path(item.path), st
                count += 1
                if count >= limit:
                    return


def read_file(path: Path, st: os.stat_result) -> tuple[str, int, int, str, str]:
    """(상태, 쪽 수, 읽은 쪽 수, 제목, 글). 글 파일은 그대로, 문서는 docs.full_text."""
    from glados.hands import ToolError, docs
    from glados.hands.fs import _decode, _is_cloud_only

    if _is_cloud_only(path):
        return "cloud", 0, 0, "", ""               # 읽으면 내려받아진다: 건너뛰고 다음에 다시 본다
    ext = path.suffix.lower().lstrip(".")
    if ext in DOC_TYPES:
        ft = docs.full_text(path, FILE_BUDGET_S, FILE_CHARS)
        return ft.status, ft.pages, ft.read_pages, ft.title, ft.text
    if st.st_size > TEXT_MAX_BYTES:
        return "too_large", 0, 0, "", ""
    try:
        text, _ = _decode(path.read_bytes())
    except (ToolError, OSError):
        return "error", 0, 0, "", ""
    from glados.core.textfix import clean

    return "ok", 0, 0, "", clean(text)[:FILE_CHARS]


def index_exts() -> set[str]:
    from glados.hands.fs import TEXT_EXTS

    return set(DOC_TYPES) | set(TEXT_EXTS)


def run_pass(idx: FileIndex, roots: list[str], places: Any, stop: threading.Event | None = None,
             beat: Callable[[], None] | None = None) -> dict[str, int]:
    """한 번 훑기: 바뀐 파일을 최근 것부터 읽고, 없어진 파일은 지운다. 멈추라고 하면 거기서 그만."""
    stop = stop or threading.Event()
    started = time.time()
    idx.set_meta(state="walking", pass_started=started)
    found = sorted(walk(roots, places, index_exts()), key=lambda f: -f[1].st_mtime)
    known = idx.entries([file_key(p) for p, _ in found])
    todo = [(p, st) for p, st in found if not (known.get(file_key(p)) and known[file_key(p)].fresh(st)
                                                and known[file_key(p)].status != "cloud")]
    idx.set_meta(state="reading", pass_total=len(todo), pass_done=0, seen=len(found))
    done = 0
    last_beat = time.monotonic()
    for path, st in todo:
        if stop.is_set():
            break
        try:
            status, pages, read_pages, title, text = read_file(path, st)
        except Exception as e:                        # 한 파일 때문에 색인 전체가 멈추지 않게
            status, pages, read_pages, title, text = "error", 0, 0, "", ""
            idx.set_meta(last_error=f"{path.name}: {type(e).__name__}: {e}"[:300])
        try:
            idx.put(path, st, status, pages, read_pages, title, text)
        except (sqlite3.Error, ValueError) as e:
            idx.set_meta(last_error=f"{path.name}: {type(e).__name__}: {e}"[:300])
        done += 1
        if time.monotonic() - last_beat > 2:
            idx.set_meta(pass_done=done)
            if beat is not None:
                beat()
            last_beat = time.monotonic()
    if not stop.is_set():                              # 다 훑었을 때만: 없어진 파일, 보호 위치가 된 것, 뺀 폴더의 것을 지운다
        seen = {file_key(p) for p, _ in found}
        gone = [fid for k, fid in idx.all_ids().items() if k not in seen]
        if gone:
            idx.remove(gone)
        idx.set_meta(state="idle", pass_done=done, pass_finished=time.time(), pass_seconds=int(time.time() - started))
    return {"seen": len(found), "read": done}


# ---------------------------------------------------------------------- 뜻으로 찾기: 조각과 벡터 (M7.2)
def chunks(text: str, size: int = CHUNK_CHARS, most: int = MAX_CHUNKS) -> list[tuple[int, str]]:
    """[(시작 위치, 조각)]: 첫머리 하나 + 나머지는 글 전체에 고르게. 쪽 표시는 빈칸으로."""
    if not text.strip():
        return []
    starts = list(range(0, len(text), size))
    if len(starts) > most:
        step = (len(starts) - 1) / (most - 1)
        starts = sorted({starts[round(i * step)] for i in range(most)})
    out = []
    for start in starts:
        piece = " ".join(text[start:start + size].replace(PAGE, " ").split())
        if piece:
            out.append((start, piece))
    return out


def embed_pass(idx: FileIndex, embedder: Any, busy: Callable[[], bool], stop: threading.Event | None = None,
               beat: Callable[[], None] | None = None, batch: int = 50) -> int:
    """벡터가 없는 파일에 벡터를 만든다 (최근 파일부터). GLaDOS가 작업 중이면 쉰다. 만든 파일 수."""
    from glados.core.llm import LLMError

    stop = stop or threading.Event()
    made = 0
    model = embedder.model
    failed: set[int] = set()                           # 이번에 못 만든 파일 (다음 훑기에 다시)
    while not stop.is_set():
        todo = [x for x in idx.needs_vectors(model, batch + len(failed)) if x[0] not in failed]
        if not todo:
            break
        idx.set_meta(embed_state="embedding", embed_left=len(todo))
        for fid, title in todo:
            while busy() and not stop.is_set():
                idx.set_meta(embed_state="waiting")
                if beat is not None:
                    beat()
                stop.wait(BUSY_WAIT_S)
            if stop.is_set():
                break
            pieces = chunks(idx.body(fid))
            try:
                vectors = embedder.documents([p for _, p in pieces], title) if pieces else []
                idx.put_vectors(fid, model, pieces, vectors)
            except LLMError as e:                      # 모델이 없거나 Ollama가 꺼짐: 이번 훑기는 여기까지
                idx.set_meta(embed_state="missing" if e.key == "model.missing" else "error",
                             embed_error=f"{type(e).__name__}: {e}"[:300])
                return made
            except Exception as e:                     # 이 파일만 건너뛴다
                failed.add(fid)
                idx.set_meta(embed_error=f"{type(e).__name__}: {e}"[:300])
                continue
            made += 1
            if beat is not None and made % 10 == 0:
                beat()
    if not stop.is_set():
        idx.set_meta(embed_state="idle", embed_error="")
    return made


# ---------------------------------------------------------------------- 색인 프로세스
def _places_from(data: dict[str, Any]) -> Any:
    from glados.safety.locations import Places

    fields = dict(data)
    for name in ("source_dirs", "extra_protected"):
        fields[name] = tuple(fields.get(name) or ())
    fields["env"] = tuple(tuple(x) for x in fields.get("env") or ())
    return Places(**fields)


def _lower_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes

            kernel = ctypes.windll.kernel32                # type: ignore[attr-defined]
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """색인 프로세스: GLaDOS가 띄운다. 표준 입력이 닫히면(GLaDOS가 꺼지면) 끝난다."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 2
    home = Path(args[0])
    try:
        job = json.loads((home / "cache" / JOB_FILE).read_text(encoding="utf-8"))
        places = _places_from(job["places"])
        roots = [str(r) for r in job.get("roots") or []]
        host, embed_model = str(job.get("host") or ""), str(job.get("embed_model") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return 2
    busy_file = home / "cache" / BUSY_FILE
    stop = threading.Event()

    def wait_parent() -> None:
        try:
            sys.stdin.buffer.read()
        except (OSError, ValueError, AttributeError):
            pass
        stop.set()

    threading.Thread(target=wait_parent, daemon=True).start()
    _lower_priority()
    if not fts5_ok():
        return 3
    idx = FileIndex(home)
    try:
        if not idx.claim(os.getpid()):
            return 0                                   # 다른 GLaDOS의 색인이 돌고 있다
        idx.set_meta(roots=json.dumps(roots, ensure_ascii=False), last_error="")

        def beat() -> None:
            idx.set_meta(beat=time.time(), pid=os.getpid())

        embedder = None
        if host and embed_model:
            from glados.core.llm import Embedder

            embedder = Embedder(host, embed_model)
        idx.set_meta(embed_model=embed_model, embed_state="off" if embedder is None else "waiting")
        while not stop.is_set():
            beat()
            try:
                run_pass(idx, roots, places, stop, beat)
                if embedder is not None:
                    embed_pass(idx, embedder, busy_file.exists, stop, beat)
            except Exception as e:                     # 색인이 멈추지 않게: 적어 두고 다음 훑기에 다시
                idx.set_meta(state="error", last_error=f"{type(e).__name__}: {e}"[:300])
            waited = 0.0
            while waited < RESCAN_S and not stop.is_set():
                beat()
                stop.wait(BEAT_S)
                waited += BEAT_S
        idx.set_meta(state="stopped", beat=0)
    finally:
        idx.close()
    return 0


# ---------------------------------------------------------------------- GLaDOS 쪽
class IndexProcess:
    """GLaDOS가 켜져 있는 동안 도는 색인 프로세스. 끝낼 때 표준 입력을 닫으면 따라 끝난다."""

    def __init__(self, home: Path, roots: list[str], places: Any, host: str = "", embed_model: str = "",
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.home = home
        cache = home / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        job = {"roots": roots, "places": asdict(places), "host": host, "embed_model": embed_model}
        (cache / JOB_FILE).write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        self.busy_file = cache / BUSY_FILE
        self.busy(False)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = popen([sys.executable, "-m", "glados.hands.index", str(home)], stdin=subprocess.PIPE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)

    def busy(self, on: bool) -> None:
        """작업 중 표시 (색인이 GPU를 쓰는 일을 미룬다, M7.2)."""
        try:
            if on:
                self.busy_file.touch()
            else:
                self.busy_file.unlink(missing_ok=True)
        except OSError:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, wait_s: float = 3.0) -> None:
        self.busy(False)
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(wait_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()


if __name__ == "__main__":
    sys.exit(main())
