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
- 모든 드라이브 (M7.5): folders.toml search_default가 ["*"]면 붙어 있는 모든 드라이브(Windows: 고정·이동식 +
  구글 드라이브, 리눅스·맥: 홈 + 붙인 볼륨)를 훑는다. 모든 파일의 이름·크기·수정 시각을 적어 두어 색인이 파일 목록도
  된다 (fs.search가 큰 범위를 디스크를 다시 훑지 않고 여기서 찾는다). 글은 문서·글 파일만
- 사진과 스캔 (M7.5): 그림 파일(20KB 이상)과 글자 없는 pdf는 GLaDOS가 놀 때 지금 모델(그림을 읽음)이 본다.
  그림은 "무엇의 사진인지 한 문장 + 보이는 글", 스캔 pdf는 앞 2쪽의 글. 그 글을 색인에 넣어 내용으로 찾게 한다.
  작업 중에는 쉰다. 클라우드 파일([index] cloud)도 내려받아 읽는다 (연구원님 결정, 2026-11-12)
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
VERSION = 2                      # 글을 뽑는 방법이 바뀌면 올린다 (그 파일들을 다시 읽음). 2: 모든 파일 목록 (M7.5)
FILE_CHARS = 400_000             # 한 파일에서 색인할 글 (긴 책은 앞부분)
FILE_BUDGET_S = 30.0             # 한 파일에 쓰는 시간 (넘으면 읽은 쪽까지)
TEXT_MAX_BYTES = 2_000_000       # 글 파일은 이보다 크면 색인하지 않는다 (로그 등)
MAX_FILES = 1_000_000            # 한 번 훑을 때 볼 파일 수 한도 (모든 드라이브)
RESCAN_S = 600.0                 # 다시 훑는 간격
BEAT_S = 20.0                    # 색인 프로세스가 살아 있다고 적는 간격
STALE_S = 90.0                   # 이보다 오래 소식이 없으면 그 색인 프로세스는 없는 것
MIN_TRIGRAM = 3                  # trigram 색인으로 찾을 수 있는 가장 짧은 낱말
PAGE = "\f"
DOC_TYPES = ("pdf", "docx", "hwp", "hwpx")
CHUNK_CHARS = 1000               # 뜻으로 찾기: 한 조각 길이
MAX_CHUNKS = 8                   # 파일마다 조각 수 (첫머리 + 고르게)
BUSY_WAIT_S = 5.0                # 작업 중이면 이만큼 쉬고 다시 본다
IMAGE_TYPES = ("png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff")
MIN_IMAGE_BYTES = 20_000         # 이보다 작은 그림(아이콘 등)은 보지 않는다
PHOTO_TYPES = ("jpg", "jpeg", "webp")   # 사진기·휴대폰 사진 형식: 먼저 본다 (png 등은 프로그램 그림이 많다)
EMBED_SLICE_S = 90.0             # GPU 일꾼이 한 번에 벡터 만들기에 쓰는 시간 (그다음 사진)
GPU_IDLE_S = 30.0                # GPU 일꾼이 할 일이 없을 때 쉬는 시간
OCR_PAGES = 2                    # 스캔 pdf: 뒤에서 미리 읽을 앞쪽 수
OCR_SLICE_S = 300.0              # 한 번에 글자 인식에 쓰는 시간 (그다음 다시 훑기·벡터를 챙긴다)
NAME_BATCH = 2000                # 이름만 적는 파일을 한 번에 몇 개씩 쓸지

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE, path TEXT, size INTEGER, mtime INTEGER, version INTEGER,
    status TEXT, pages INTEGER, read_pages INTEGER, title TEXT, chars INTEGER, indexed REAL,
    vec_model TEXT DEFAULT '', ext TEXT DEFAULT '', ocr INTEGER DEFAULT 0)""",
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


def _ext(path: str | Path) -> str:
    name = Path(path).name
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


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
        for name, kind in (("vec_model", "TEXT DEFAULT ''"), ("ext", "TEXT DEFAULT ''"), ("ocr", "INTEGER DEFAULT 0")):
            if name not in columns:                    # 예전 색인 (M7.1·M7.2): 칸을 더한다 (VERSION이 달라 다시 읽음)
                self.con.execute(f"ALTER TABLE files ADD COLUMN {name} {kind}")

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
            title: str = "", text: str = "", ocr: int = 0) -> int:
        key = file_key(path)
        text = nfc(text)
        values = (str(path), st.st_size, st.st_mtime_ns, VERSION, status, pages, read_pages, nfc(title), len(text),
                  time.time(), _ext(path), ocr)
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                row = self.con.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
                if row:
                    fid = int(row[0])
                    self.con.execute("UPDATE files SET path = ?, size = ?, mtime = ?, version = ?, status = ?, pages = ?, "
                                     "read_pages = ?, title = ?, chars = ?, indexed = ?, ext = ?, ocr = ? WHERE id = ?",
                                     (*values, fid))
                    self.con.execute("DELETE FROM texts WHERE rowid = ?", (fid,))
                    self.con.execute("DELETE FROM vecs WHERE file_id = ?", (fid,))
                    self.con.execute("UPDATE files SET vec_model = '' WHERE id = ?", (fid,))
                else:
                    fid = int(self.con.execute(
                        "INSERT INTO files (path, size, mtime, version, status, pages, read_pages, title, chars, indexed, "
                        "ext, ocr, key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, key)).lastrowid)
                if text:
                    self.con.execute("INSERT INTO texts (rowid, body) VALUES (?, ?)", (fid, text))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return fid

    def put_names(self, items: list[tuple[Path, os.stat_result, str]]) -> None:
        """이름만 적는 파일들 (글은 없음): (경로, stat, 상태). 바뀐 파일이면 예전 글·벡터를 지운다."""
        now = time.time()
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for path, st, status in items:
                    key = file_key(path)
                    row = self.con.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
                    values = (str(path), st.st_size, st.st_mtime_ns, VERSION, status, now, _ext(path))
                    if row:
                        fid = int(row[0])
                        self.con.execute("UPDATE files SET path = ?, size = ?, mtime = ?, version = ?, status = ?, pages = 0, "
                                         "read_pages = 0, title = '', chars = 0, indexed = ?, ext = ?, ocr = 0, "
                                         "vec_model = '' WHERE id = ?", (*values, fid))
                        self.con.execute("DELETE FROM texts WHERE rowid = ?", (fid,))
                        self.con.execute("DELETE FROM vecs WHERE file_id = ?", (fid,))
                    else:
                        self.con.execute("INSERT INTO files (path, size, mtime, version, status, pages, read_pages, title, "
                                         "chars, indexed, ext, ocr, key) VALUES (?, ?, ?, ?, ?, 0, 0, '', 0, ?, ?, 0, ?)",
                                         (*values, key))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    def known(self) -> dict[str, tuple[int, int, int, str]]:
        """{키: (크기, 수정 시각 ns, VERSION, 상태)} — 훑을 때 바뀐 파일만 고르려고."""
        with self._lock:
            return {r[0]: (r[1], r[2], r[3], r[4])
                    for r in self.con.execute("SELECT key, size, mtime, version, status FROM files")}

    def catalog(self, root: str, exts: tuple[str, ...] = ()) -> list[Entry]:
        """root 아래 파일들 (파일 목록, M7.5). exts가 있으면 그 종류만."""
        prefix = file_key(root)
        if not prefix.endswith(("\\", "/")):
            prefix += os.sep
        sql = ("SELECT id, key, path, size, mtime, version, status, pages, read_pages, title FROM files "
               "WHERE key >= ? AND key < ?")
        args: list[Any] = [prefix, prefix + "\U0010ffff"]
        if exts:
            sql += f" AND ext IN ({', '.join('?' * len(exts))})"
            args += list(exts)
        with self._lock:
            return [Entry(*r) for r in self.con.execute(sql, args)]

    # ------------------------------------------------------------------ 범위 안에서 (큰 범위 찾기, M7.5)
    @staticmethod
    def _scope_sql(prefixes: list[str], exts: tuple[str, ...]) -> tuple[str, list[Any]]:
        """key가 prefixes 가운데 하나로 시작하고 (exts면 그 종류인) 파일."""
        parts = " OR ".join("(key >= ? AND key < ?)" for _ in prefixes)
        args: list[Any] = []
        for pre in prefixes:
            args += [pre, pre + "\U0010ffff"]
        sql = f"({parts})" if prefixes else "1"
        if exts:
            sql += f" AND ext IN ({', '.join('?' * len(exts))})"
            args += list(exts)
        return sql, args

    def rows(self, ids: list[int]) -> list[Entry]:
        out: list[Entry] = []
        with self._lock:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                out += [Entry(*r) for r in self.con.execute(
                    "SELECT id, key, path, size, mtime, version, status, pages, read_pages, title FROM files "
                    f"WHERE id IN ({', '.join('?' * len(part))})", part)]
        return out

    def scope_counts(self, prefixes: list[str], exts: tuple[str, ...] = ()) -> dict[str, Any]:
        """범위 안 파일 수: {"status": {상태: 수}, "partial": 수, "names": {확장자: 수} (이름만 있는 것, 많은 순 5개)}."""
        where, args = self._scope_sql(prefixes, exts)
        with self._lock:
            by = dict(self.con.execute(f"SELECT status, COUNT(*) FROM files WHERE {where} GROUP BY status", args))
            partial = self.con.execute(f"SELECT COUNT(*) FROM files WHERE {where} AND status = 'ok' "
                                       "AND read_pages < pages", args).fetchone()[0]
            names = dict(self.con.execute(f"SELECT ext, COUNT(*) AS n FROM files WHERE {where} AND status = 'name' "
                                          "GROUP BY ext ORDER BY n DESC LIMIT 5", args))
        return {"status": by, "partial": int(partial), "names": names}

    def scope_rows(self, prefixes: list[str], exts: tuple[str, ...], statuses: tuple[str, ...],
                   limit: int = 300) -> list[Entry]:
        where, args = self._scope_sql(prefixes, exts)
        sql = (f"SELECT id, key, path, size, mtime, version, status, pages, read_pages, title FROM files WHERE {where} "
               f"AND status IN ({', '.join('?' * len(statuses))}) ORDER BY mtime DESC LIMIT ?")
        with self._lock:
            return [Entry(*r) for r in self.con.execute(sql, (*args, *statuses, limit))]

    def nearest_in(self, prefixes: list[str], exts: tuple[str, ...], query: list[float], model: str,
                   exclude: set[int] = frozenset()) -> tuple[list[tuple[int, float, int]], int, int]:
        """범위 안에서 질문 벡터와 가까운 파일 ([(id, 닮은 정도, 조각 시작)], 벡터가 있는 파일 수, 글이 있는 파일 수)."""
        import numpy as np

        q = np.asarray(query, dtype=np.float32)
        q /= float(np.linalg.norm(q)) or 1.0
        where, args = self._scope_sql(prefixes, exts)
        with self._lock:
            ready = self.con.execute(f"SELECT COUNT(*) FROM files WHERE {where} AND vec_model = ?", (*args, model)).fetchone()[0]
            total = self.con.execute(f"SELECT COUNT(*) FROM files WHERE {where} AND status = 'ok' AND chars > 0",
                                     args).fetchone()[0]
            rows = self.con.execute(f"SELECT v.file_id, v.start, v.vec FROM vecs v JOIN files f ON f.id = v.file_id "
                                    f"WHERE f.vec_model = ? AND {where.replace('key', 'f.key').replace('ext IN', 'f.ext IN')}",
                                    (model, *args)).fetchall()
        kept = [r for r in rows if len(r[2]) == q.size * 2 and r[0] not in exclude]
        best: dict[int, tuple[float, int]] = {}
        if kept:
            mat = np.frombuffer(b"".join(r[2] for r in kept), dtype=np.float16).reshape(len(kept), q.size)
            scores = mat.astype(np.float32) @ q
            for (fid, start, _), score in zip(kept, scores.tolist()):
                if fid not in best or score > best[fid][0]:
                    best[fid] = (score, int(start))
        near = sorted(((fid, sc, st) for fid, (sc, st) in best.items()), key=lambda x: -x[1])
        return near, int(ready), int(total)

    def needs_ocr(self, limit: int = 20, first: list[str] | None = None) -> list[tuple[int, str, str, int, int]]:
        """글자 인식을 기다리는 파일: [(id, 경로, 상태, 크기, 수정 시각)]. first(폴더 키들) 아래 것 먼저, 사진 형식
        (jpg 등) 먼저, 그다음 최근 순 (M7.6: 4천 장이 최근 순이면 프로그램의 그림들이 연구원님 사진보다 먼저였다)."""
        first = first or []
        inside = " OR ".join("(key >= ? AND key < ?)" for _ in first) or "0"
        args: list[Any] = []
        for pre in first:
            args += [pre, pre + "\U0010ffff"]
        with self._lock:
            rows = self.con.execute(
                "SELECT id, path, status, size, mtime FROM files WHERE ocr = 0 AND version = ? AND "
                "(status = 'image' OR (status = 'no_text' AND ext = 'pdf')) "
                f"ORDER BY CASE WHEN {inside} THEN 0 ELSE 1 END, "
                f"CASE WHEN ext IN ({', '.join('?' * len(PHOTO_TYPES))}) THEN 0 ELSE 1 END, mtime DESC LIMIT ?",
                (VERSION, *args, *PHOTO_TYPES, limit)).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in rows]

    def mark_ocr(self, fid: int, state: int) -> None:
        with self._lock:
            self.con.execute("UPDATE files SET ocr = ? WHERE id = ?", (state, fid))

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
        세 글자 이상은 trigram 색인으로, 짧은 낱말이 낀 묶음은 글에서 직접 (LIKE). 맞는 정도(bm25) 순서."""
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
            order = " ORDER BY rank" if fts else ""            # 맞는 정도(bm25) 순서: 짧은 글에 낱말이 있으면 위로 (M7.6)
            rows = self.con.execute(f"SELECT rowid FROM texts WHERE {' AND '.join(where)}{order} LIMIT ?",
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
            ocr_left = self.con.execute("SELECT COUNT(*) FROM files WHERE ocr = 0 AND (status = 'image' OR "
                                        "(status = 'no_text' AND ext = 'pdf'))").fetchone()[0]
            ocr_done = self.con.execute("SELECT COUNT(*) FROM files WHERE ocr = 1").fetchone()[0]
            meta = dict(self.con.execute("SELECT k, v FROM meta").fetchall())
        return {"by_status": by, "files": sum(by.values()), "partial": int(partial), "vectors": vec,
                "ocr_left": int(ocr_left), "ocr_done": int(ocr_done), **meta}


def open_index(home: Path) -> FileIndex | None:
    """찾기에서 쓸 색인 (없거나 못 열면 None: 예전처럼 그 자리에서 읽는다)."""
    if not index_path(home).exists() or not fts5_ok():
        return None
    try:
        return FileIndex(home)
    except (sqlite3.Error, OSError):
        return None


# ---------------------------------------------------------------------- 훑기 (색인 프로세스)
GOOGLE_MARKS = ("내 드라이브", "My Drive", "공유 드라이브", "Shared drives", ".shortcut-targets-by-id")


def all_roots(os_name: str, home: str) -> list[str]:
    """"모든 드라이브" (folders.toml search_default의 "*"): Windows는 고정·이동식 드라이브와 구글 드라이브,
    리눅스·맥은 홈과 붙인 볼륨 (/ 전체는 시스템 파일뿐이라 훑지 않는다)."""
    if os_name == "windows":
        drives: list[str] = []
        try:
            import ctypes

            kernel = ctypes.windll.kernel32                # type: ignore[attr-defined]
            mask = kernel.GetLogicalDrives()
            for i in range(26):
                if not mask & (1 << i):
                    continue
                d = f"{chr(65 + i)}:\\"
                kind = kernel.GetDriveTypeW(d)             # 2 이동식, 3 고정, 4 네트워크, 5 CD
                google = any(os.path.exists(os.path.join(d, m)) for m in GOOGLE_MARKS) if kind in (3, 4) else False
                if kind in (2, 3) or google:
                    drives.append(d)
        except (OSError, AttributeError):
            drives = [d for d in (os.environ.get("SystemDrive", "C:") + "\\",) if os.path.isdir(d)]
        return drives
    roots = [home]
    bases = ["/Volumes"] if os_name == "mac" else ["/media", "/run/media", "/mnt"]
    for base in bases:
        try:
            for item in os.scandir(base):
                if not item.is_dir():
                    continue
                if os_name == "mac" and os.path.realpath(item.path) == "/":
                    continue                               # /Volumes/Macintosh HD = /
                inner = [item.path]
                if base in ("/media", "/run/media"):       # /media/<사용자>/<볼륨>
                    try:
                        inner = [x.path for x in os.scandir(item.path) if x.is_dir()] or [item.path]
                    except OSError:
                        pass
                roots += [x for x in inner if os.path.ismount(x) or base == "/mnt"]
        except OSError:
            continue
    return list(dict.fromkeys(roots))


def _hidden_system(item: os.DirEntry) -> bool:
    """Windows의 숨김·시스템 폴더 ($Recycle.Bin, System Volume Information ...)."""
    try:
        attrs = getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & 0x2 or attrs & 0x4)


def walk(roots: list[str], places: Any, exts: set[str] | None = None,
         limit: int = MAX_FILES) -> Iterator[tuple[Path, os.stat_result]]:
    """색인할 파일 (exts가 없으면 모든 파일): 숨은 폴더·$로 시작하는 폴더·SKIP_DIRS·안전장치·보호 위치는 건너뛴다
    (찾기와 같은 규칙). 바로가기 폴더(정션·링크)도 따라가되 같은 폴더를 두 번 보지 않는다."""
    from glados.hands.fs import SKIP_DIRS
    from glados.safety.locations import classify

    skip = {d.casefold() for d in SKIP_DIRS}
    seen: set[str] = set()
    folders_seen: set[str] = set()
    count = 0
    for root in roots:
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                real = os.path.realpath(folder)
            except (OSError, ValueError):
                real = folder
            if file_key(real) in folders_seen:
                continue
            folders_seen.add(file_key(real))
            try:
                items = list(os.scandir(folder))
            except OSError:
                continue
            for item in items:
                name = item.name
                if name.startswith((".", "$")) or name.casefold() in ("system volume information",):
                    continue
                try:
                    if item.is_dir():
                        if name.casefold() in skip or (os.name == "nt" and _hidden_system(item)):
                            continue
                        loc = classify(item.path, places)
                        if not (loc.safety or loc.protected):
                            stack.append(item.path)
                        continue
                    if not item.is_file():
                        continue
                except OSError:
                    continue
                if exts is not None:
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


def read_file(path: Path, st: os.stat_result, cloud: bool = False) -> tuple[str, int, int, str, str]:
    """(상태, 쪽 수, 읽은 쪽 수, 제목, 글). 글 파일은 그대로, 문서는 docs.full_text.
    cloud=False면 클라우드에만 있는 파일은 내려받지 않고 건너뛴다 (다음에 다시 본다)."""
    from glados.hands import ToolError, docs
    from glados.hands.fs import _decode, _is_cloud_only

    if not cloud and _is_cloud_only(path):
        return "cloud", 0, 0, "", ""
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
    """글을 읽어 색인하는 종류 (나머지는 이름만)."""
    from glados.hands.fs import TEXT_EXTS

    return set(DOC_TYPES) | set(TEXT_EXTS)


def name_status(path: Path, st: os.stat_result) -> str:
    """글을 읽지 않는 파일의 상태: 그림(20KB 이상)은 글자 인식을 기다리는 image, 나머지는 name."""
    return "image" if _ext(path) in IMAGE_TYPES and st.st_size >= MIN_IMAGE_BYTES else "name"


def run_pass(idx: FileIndex, roots: list[str], places: Any, stop: threading.Event | None = None,
             beat: Callable[[], None] | None = None, cloud: bool = False) -> dict[str, int]:
    """한 번 훑기: 먼저 바뀐 파일의 이름을 모두 적고 (파일 목록), 그다음 문서·글 파일을 최근 것부터 읽는다.
    없어진 파일은 지운다. 멈추라고 하면 거기서 그만."""
    stop = stop or threading.Event()
    started = time.time()
    idx.set_meta(state="walking", pass_started=started)
    readable = index_exts()
    found = sorted(walk(roots, places), key=lambda f: -f[1].st_mtime)
    known = idx.known()

    def fresh(p: Path, st: os.stat_result) -> tuple | None:
        k = known.get(file_key(p))
        return k if k and k[0] == st.st_size and k[1] == st.st_mtime_ns and k[2] == VERSION else None

    names: list[tuple[Path, os.stat_result, str]] = []
    todo: list[tuple[Path, os.stat_result]] = []
    for p, st in found:
        k = fresh(p, st)
        if _ext(p) in readable:
            if k is None or k[3] in ("pending", "cloud"):
                todo.append((p, st))
                if k is None:
                    names.append((p, st, "pending"))           # 읽기 전에도 이름으로는 찾게
        elif k is None:
            names.append((p, st, name_status(p, st)))
    idx.set_meta(state="listing", pass_total=len(names), pass_done=0, seen=len(found))
    for i in range(0, len(names), NAME_BATCH):
        if stop.is_set():
            break
        idx.put_names(names[i:i + NAME_BATCH])
        idx.set_meta(pass_done=min(i + NAME_BATCH, len(names)))
        if beat is not None:
            beat()
    if not stop.is_set():
        idx.set_meta(catalog_finished=time.time())             # 파일 목록이 다 찼다 (찾기가 써도 된다)
    idx.set_meta(state="reading", pass_total=len(todo), pass_done=0)
    done = 0
    last_beat = time.monotonic()
    for path, st in todo:
        if stop.is_set():
            break
        try:
            status, pages, read_pages, title, text = read_file(path, st, cloud)
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
    return {"seen": len(found), "read": done, "listed": len(names)}


# ---------------------------------------------------------------------- 사진·스캔 글자 인식 (M7.5)
def ocr_pass(idx: FileIndex, seer: Any, model: str, busy: Callable[[], bool], stop: threading.Event | None = None,
             beat: Callable[[], None] | None = None, budget_s: float = OCR_SLICE_S, cache: Any = None,
             first: list[str] | None = None) -> int:
    """그림과 스캔 pdf를 지금 모델로 본다 (최근 것부터, 작업 중이면 쉼). 그림은 "무엇의 사진인지 + 보이는 글",
    스캔 pdf는 앞 OCR_PAGES쪽의 글. budget_s가 지나면 멈춘다 (다음 차례에 이어서). 본 파일 수."""
    from glados.core.llm import LLMError
    from glados.hands import ocr

    stop = stop or threading.Event()
    started = time.monotonic()
    made = 0
    failed: set[int] = set()
    while not stop.is_set() and time.monotonic() - started < budget_s:
        todo = [x for x in idx.needs_ocr(20 + len(failed), first) if x[0] not in failed]
        if not todo:
            break
        for fid, path_s, status, size, mtime in todo:
            if stop.is_set() or time.monotonic() - started >= budget_s:
                break
            while busy() and not stop.is_set():
                idx.set_meta(ocr_state="waiting")
                if beat is not None:
                    beat()
                stop.wait(BUSY_WAIT_S)
            if stop.is_set():
                break
            path = Path(path_s)
            try:
                st = path.stat()
            except OSError:
                failed.add(fid)
                idx.mark_ocr(fid, 2)
                continue
            if st.st_size != size or st.st_mtime_ns != mtime:
                failed.add(fid)                       # 그 사이에 바뀜: 다음 훑기가 다시 적는다
                continue
            idx.set_meta(ocr_state="reading")
            try:
                if status == "image":
                    text = ocr.describe_cached(seer, path, st, model, cache, max_side=ocr.BACKGROUND_SIDE)
                    idx.put(path, st, "ok", text=text, ocr=1)
                else:
                    n = _pdf_pages(path)
                    pages = [ocr.page_cached(seer, path, st, i, model, cache) for i in range(min(n, OCR_PAGES))]
                    body = PAGE.join(pages)
                    has = len("".join(body.split())) >= 3
                    idx.put(path, st, "ok" if has else "no_text", n, len(pages), text=body if has else "", ocr=1)
            except LLMError as e:                      # 모델이 없거나 그림을 못 읽음, Ollama가 꺼짐: 이번 차례는 여기까지
                idx.set_meta(ocr_state="missing" if e.key in ("model.missing", "llm.no_vision") else "error",
                             ocr_error=f"{type(e).__name__}: {e}"[:300])
                return made
            except Exception as e:                     # 열 수 없는 그림 (heic 등): 이 파일만 건너뛴다
                failed.add(fid)
                idx.mark_ocr(fid, 2)
                idx.set_meta(ocr_error=f"{path.name}: {type(e).__name__}: {e}"[:300])
                continue
            made += 1
            if beat is not None and made % 5 == 0:
                beat()
    if not stop.is_set():
        idx.set_meta(ocr_state="idle" if not idx.needs_ocr(1) else "paused")
    return made


def gpu_work(idx: FileIndex, embedder: Any, seer: Any, model: str, busy: Callable[[], bool], stop: threading.Event,
             cache: Any = None, first: list[str] | None = None, idle_s: float = GPU_IDLE_S) -> None:
    """모델(GPU)을 쓰는 일: 사진·스캔 보기와 벡터 만들기를 번갈아. 글 읽기(CPU)와 따로 돈다 (M7.6: 전에는 글을 다
    읽은 뒤에야 사진을 보기 시작해서, 첫날 사진은 하나도 못 봤다). 할 일이 없으면 잠깐 쉬었다가 다시 본다."""
    while not stop.is_set():
        did = 0
        try:
            if seer is not None:
                did += ocr_pass(idx, seer, model, busy, stop, None, OCR_SLICE_S, cache, first)
            if embedder is not None:
                did += embed_pass(idx, embedder, busy, stop, None, budget_s=EMBED_SLICE_S)
        except Exception as e:                         # 일꾼이 멈추지 않게
            idx.set_meta(gpu_error=f"{type(e).__name__}: {e}"[:300])
        if not did:
            stop.wait(idle_s)


def _pdf_pages(path: Path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 1


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
               beat: Callable[[], None] | None = None, batch: int = 50, budget_s: float | None = None) -> int:
    """벡터가 없는 파일에 벡터를 만든다 (최근 파일부터). GLaDOS가 작업 중이면 쉰다. budget_s가 지나면 멈춘다
    (다음 차례에 이어서). 만든 파일 수."""
    from glados.core.llm import LLMError

    stop = stop or threading.Event()
    started = time.monotonic()
    made = 0
    model = embedder.model
    failed: set[int] = set()                           # 이번에 못 만든 파일 (다음 훑기에 다시)
    while not stop.is_set():
        todo = [x for x in idx.needs_vectors(model, batch + len(failed)) if x[0] not in failed]
        if not todo:
            break
        idx.set_meta(embed_state="embedding", embed_left=len(todo))
        for fid, title in todo:
            if budget_s is not None and time.monotonic() - started > budget_s:
                idx.set_meta(embed_state="paused")
                return made
            while busy() and not stop.is_set():
                idx.set_meta(embed_state="waiting")
                if beat is not None:
                    beat()
                stop.wait(BUSY_WAIT_S)
            if stop.is_set():
                break
            idx.set_meta(embed_state="embedding")
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
        cloud = bool(job.get("cloud", False))
        vision = job.get("vision") or {}                   # 사진·스캔을 볼 모델 (M7.5): {name, num_ctx, keep_alive}
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
        seer = cache = None
        if host and vision.get("name"):
            from glados.config import ModelConfig
            from glados.core.llm import OllamaClient
            from glados.hands.docs import DocCache

            seer = OllamaClient(ModelConfig(host=host, name=str(vision["name"]), num_ctx=int(vision.get("num_ctx") or 16384),
                                            keep_alive=str(vision.get("keep_alive") or "30m")))
            cache = DocCache(home / "cache" / "doctext.sqlite")
        idx.set_meta(embed_model=embed_model, embed_state="off" if embedder is None else "ready",
                     ocr_state="off" if seer is None else "ready")
        first = [file_key(x) if file_key(x).endswith(("\\", "/")) else file_key(x) + os.sep
                 for x in job.get("first") or []]
        if embedder is not None or seer is not None:   # 모델 일은 따로 (글 읽기와 같이 돈다)
            threading.Thread(target=gpu_work, args=(idx, embedder, seer, str(vision.get("name") or ""),
                                                    busy_file.exists, stop, cache, first), daemon=True).start()
        while not stop.is_set():
            beat()
            try:
                run_pass(idx, roots, places, stop, beat, cloud)
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
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, cloud: bool = False,
                 vision: dict[str, Any] | None = None, first: list[str] | None = None):
        self.home = home
        cache = home / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        job = {"roots": roots, "places": asdict(places), "host": host, "embed_model": embed_model, "cloud": cloud,
               "vision": vision or {}, "first": first or []}
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
