"""문서 내용 (찾기·살펴보기에서 씀): PDF·DOCX의 글을 뽑아 캐시한다. 무엇을 찾을지·어떤 문서인지는 판단하지 않는다.

- 뽑기: PDF는 앞 2쪽 + 끝 3쪽, 내용으로 찾을 때는 앞 12쪽까지 더. DOCX·글 파일은 전체.
  글자가 없는 PDF(스캔한 그림)는 no_text, 암호가 걸린 것은 locked
- 캐시: GLADOS_HOME/cache/doctext.sqlite. 경로·크기·수정 시각이 같으면 다시 읽지 않는다 (두 번째부터 바로).
  fs.inspect의 답(파일·질문·모델마다)도 여기 둔다. 이 컴퓨터 밖으로 나가지 않는다. 지워도 된다 (다음에 다시 읽음)
- 어떤 문서가 논문인지 같은 판단은 여기서 하지 않는다 (규칙으로 박지 않음). 모델이 질문을 정하고
  fs.inspect가 파일마다 모델에게 묻는다 (M2.7, 연구원님 결정: "하드코딩하지 말고 모델이 생각하게")
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from glados.core.textfix import clean

logging.getLogger("pypdf").setLevel(logging.ERROR)     # 글꼴 경고가 화면에 찍히지 않게

FRONT_PAGES = 2               # 앞: 제목·저자·첫 내용
BACK_PAGES = 3                # 끝
DEEP_PAGES = 12               # 내용으로 찾을 때 읽는 앞쪽 (긴 책은 앞부분만)
PART_CHARS = 12_000           # 앞·끝 글 한도
BODY_CHARS = 150_000          # 내용 찾기 글 한도
MAX_BYTES = 80_000_000        # 이보다 큰 파일은 내용을 보지 않는다
FILE_BUDGET_S = 8.0           # 한 파일에 쓰는 시간 (넘으면 읽은 데까지)
VERSION = 1                   # 뽑는 방법이 바뀌면 올린다 (캐시를 다시 채움)
DOC_EXTS = ("pdf", "docx", "hwp", "hwpx")         # hwp·hwpx는 M7.3 (hands/hwp.py)


@dataclass
class Doc:
    path: str
    status: str = "ok"            # ok | no_text | locked | error | too_large
    pages: int = 0                # PDF 쪽 수 (DOCX는 0)
    title: str = ""               # 파일 정보의 제목 (있는 그대로)
    creator: str = ""             # 만든 프로그램 (PDF 정보)
    front: str = ""
    back: str = ""
    body: str = ""                # 앞쪽과 끝쪽 사이 (deep으로 읽었을 때)
    deep: bool = False            # 내용 찾기에 쓸 만큼 다 읽었는지
    detail: str = ""

    def text(self) -> str:
        return "\n".join(p for p in (self.title, self.front, self.body, self.back) if p)


# ---------------------------------------------------------------------- 뽑기
def extract(path: Path, deep: bool = False) -> Doc:
    """PDF·DOCX의 글. 실패해도 예외 대신 status로 알린다 (찾기는 계속한다)."""
    ext = path.suffix.lower().lstrip(".")
    try:
        size = path.stat().st_size
    except OSError as e:
        return Doc(str(path), "error", detail=str(e)[:200])
    if size > MAX_BYTES:
        return Doc(str(path), "too_large")
    if ext == "pdf":
        doc = _pdf(path, deep)
    elif ext == "docx":
        doc = _docx(path)
    elif ext in ("hwp", "hwpx"):
        doc = _hwp(path)
    else:
        return Doc(str(path), "error", detail=f"not a pdf, docx, or hwp: {ext}")
    if doc.status == "ok" and len(re.sub(r"\s", "", doc.front + doc.body + doc.back)) < 3:   # 스캔한 그림뿐
        doc.status = "no_text"
    for name in ("title", "creator", "front", "back", "body", "detail"):     # 깨진 글자 (M6.2)
        setattr(doc, name, clean(getattr(doc, name)))
    return doc


def _pdf(path: Path, deep: bool) -> Doc:
    from pypdf import PdfReader

    start = time.monotonic()
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return Doc(str(path), "locked")
        n = len(reader.pages)
        info = reader.metadata or {}
    except Exception as e:
        return Doc(str(path), "error", detail=f"{type(e).__name__}: {e}"[:200])

    def page(i: int) -> str:
        try:
            return reader.pages[i].extract_text() or ""
        except Exception:
            return ""

    front_n = min(n, FRONT_PAGES)
    back_start = max(front_n, n - BACK_PAGES)
    front = [page(i) for i in range(front_n)]
    back = [page(i) for i in range(back_start, n) if time.monotonic() - start < FILE_BUDGET_S]
    body: list[str] = []
    complete = True
    if deep:
        for i in range(front_n, min(back_start, DEEP_PAGES)):
            if time.monotonic() - start > FILE_BUDGET_S:
                complete = False
                break
            body.append(page(i))
    return Doc(
        str(path), "ok", pages=n,
        title=str(info.get("/Title") or "").strip(),
        creator=" ".join(str(info.get(k) or "") for k in ("/Creator", "/Producer")).strip(),
        front="\n".join(front)[:PART_CHARS], back="\n".join(back)[-PART_CHARS:],
        body="\n".join(body)[:BODY_CHARS], deep=deep and complete,
    )


def pdf_bytes_text(data: bytes, max_pages: int, budget_s: float = 15.0) -> tuple[str, int, int, str, str]:
    """웹에서 받은 PDF의 글 (web.fetch). (글, 전체 쪽 수, 읽은 쪽 수, 제목, 상태 ok|locked|error|no_text).
    쪽마다 "[page N]" 표시를 붙인다 (요약할 때 어디서 나온 말인지 말할 수 있게)."""
    import io

    from pypdf import PdfReader

    start = time.monotonic()
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return "", 0, 0, "", "locked"
        n = len(reader.pages)
        title = str((reader.metadata or {}).get("/Title") or "").strip()
    except Exception as e:
        return "", 0, 0, "", f"error: {type(e).__name__}"[:200]
    parts: list[str] = []
    read = 0
    for i in range(min(n, max_pages)):
        if time.monotonic() - start > budget_s:
            break
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        parts.append(f"[page {i + 1}]\n{text.strip()}")
        read += 1
    body = "\n\n".join(parts)
    if len(re.sub(r"\s|\[page \d+\]", "", body)) < 3:
        return "", n, read, title, "no_text"
    return body, n, read, title, "ok"


def _docx(path: Path) -> Doc:
    from docx import Document

    try:
        d = Document(str(path))
    except Exception as e:
        return Doc(str(path), "error", detail=f"{type(e).__name__}: {e}"[:200])
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    doc = from_text(str(path), "\n".join(parts))
    doc.title = (d.core_properties.title or "").strip()
    return doc


@dataclass
class FullText:
    """색인용 글 전체 (M7.1). pdf는 쪽 사이에 PAGE_BREAK (몇 쪽에서 나왔는지 말할 수 있게)."""
    status: str = "ok"            # ok | no_text | locked | error | too_large
    pages: int = 0                # pdf 쪽 수
    read_pages: int = 0           # 읽은 쪽 수 (시간·글 한도로 덜 읽었으면 pages보다 작다)
    title: str = ""
    text: str = ""
    detail: str = ""


PAGE_BREAK = "\f"


def full_text(path: Path, budget_s: float = 30.0, max_chars: int = 400_000) -> FullText:
    """pdf·docx의 글 전체 (색인용). 실패해도 예외 대신 status로."""
    ext = path.suffix.lower().lstrip(".")
    try:
        if path.stat().st_size > MAX_BYTES:
            return FullText("too_large")
    except OSError as e:
        return FullText("error", detail=str(e)[:200])
    if ext == "pdf":
        out = _pdf_full(path, budget_s, max_chars)
    elif ext == "docx":
        doc = _docx(path)
        if doc.status != "ok":
            return FullText(doc.status, detail=doc.detail)
        out = FullText("ok", title=doc.title, text=doc.text()[:max_chars])
    elif ext in ("hwp", "hwpx"):
        from glados.hands import hwp

        h = hwp.read(path, max_chars)
        out = FullText(h.status, title=h.title, text=h.text, detail=h.detail)
    else:
        return FullText("error", detail=f"not a document: {ext}")
    if out.status == "ok" and len(re.sub(r"\s", "", out.text)) < 3:      # 스캔한 그림뿐
        out.status = "no_text"
    out.title, out.text, out.detail = clean(out.title), clean(out.text), clean(out.detail)
    return out


def _pdf_full(path: Path, budget_s: float, max_chars: int) -> FullText:
    from pypdf import PdfReader

    start = time.monotonic()
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return FullText("locked")
        n = len(reader.pages)
        title = str((reader.metadata or {}).get("/Title") or "").strip()
    except Exception as e:
        return FullText("error", detail=f"{type(e).__name__}: {e}"[:200])
    parts: list[str] = []
    size = 0
    for i in range(n):
        if time.monotonic() - start > budget_s or size >= max_chars:
            break
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        parts.append(text.replace(PAGE_BREAK, " "))
        size += len(text) + 1
    return FullText("ok", pages=n, read_pages=len(parts), title=title, text=PAGE_BREAK.join(parts)[:max_chars])


def _hwp(path: Path) -> Doc:
    from glados.hands import hwp

    h = hwp.read(path)
    if h.status != "ok":
        return Doc(str(path), h.status, detail=h.detail)
    doc = from_text(str(path), h.text)
    doc.title, doc.detail = h.title, h.detail
    return doc


def from_text(path: str, text: str) -> Doc:
    """글 전체가 있는 문서 (DOCX, 글 파일): 앞·끝·가운데로 나눠 PDF와 같은 모양으로."""
    text = clean(text)
    half = PART_CHARS // 2
    if len(text) <= PART_CHARS:
        return Doc(path, "ok", front=text, deep=True)
    return Doc(path, "ok", front=text[:half], back=text[-half:], body=text[half:-half][:BODY_CHARS], deep=True)


# ---------------------------------------------------------------------- 캐시
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS docs (
    key TEXT PRIMARY KEY, version INTEGER, size INTEGER, mtime INTEGER, status TEXT, pages INTEGER,
    title TEXT, creator TEXT, front TEXT, back TEXT, body TEXT, deep INTEGER, detail TEXT, used REAL)""",
    """CREATE TABLE IF NOT EXISTS answers (
    key TEXT, question TEXT, model TEXT, size INTEGER, mtime INTEGER, answer TEXT, why TEXT, used REAL,
    PRIMARY KEY (key, question, model))""",
    """CREATE TABLE IF NOT EXISTS ocr (
    key TEXT, page INTEGER, model TEXT, size INTEGER, mtime INTEGER, text TEXT, used REAL,
    PRIMARY KEY (key, page, model))""",
)
COLUMNS = ("status", "pages", "title", "creator", "front", "back", "body", "deep", "detail")


class DocCache:
    """뽑은 글과 살펴본 답의 캐시. 경로·크기·수정 시각(ns)이 같으면 그대로 쓴다. 못 열면 캐시 없이 돈다."""

    def __init__(self, path: Path | None):
        self._lock = threading.Lock()
        self.con: sqlite3.Connection | None = None
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(str(path), timeout=5, check_same_thread=False, isolation_level=None)
            for statement in SCHEMA:
                self.con.execute(statement)
        except (OSError, sqlite3.Error):
            self.con = None

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def _one(self, sql: str, params: tuple) -> tuple | None:
        if self.con is None:
            return None
        try:
            with self._lock:
                return self.con.execute(sql, params).fetchone()
        except (sqlite3.Error, ValueError):              # ValueError: 이름에 깨진 글자 (UnicodeEncodeError)
            return None

    def _write(self, sql: str, params: tuple) -> None:
        if self.con is None:
            return
        try:
            with self._lock:
                self.con.execute(sql, params)
        except (sqlite3.Error, ValueError):
            pass                                         # 캐시를 못 써도 찾기는 계속한다

    def get(self, path: Path, st: os.stat_result, deep: bool) -> Doc | None:
        row = self._one(f"SELECT version, size, mtime, {', '.join(COLUMNS)} FROM docs WHERE key = ?", (self._key(path),))
        if row is None or row[0] != VERSION or row[1] != st.st_size or row[2] != st.st_mtime_ns:
            return None
        doc = Doc(str(path), **dict(zip(COLUMNS, row[3:])))
        doc.deep = bool(doc.deep)
        if deep and not doc.deep and doc.status == "ok":
            return None                                  # 앞뒤만 읽어 둔 것: 내용 찾기에는 더 읽어야 한다
        return doc

    def put(self, doc: Doc, st: os.stat_result) -> None:
        values = (self._key(Path(doc.path)), VERSION, st.st_size, st.st_mtime_ns,
                  *(int(getattr(doc, c)) if c == "deep" else getattr(doc, c) for c in COLUMNS), time.time())
        self._write(f"INSERT OR REPLACE INTO docs VALUES ({', '.join('?' * len(values))})", values)

    def answer(self, path: Path, st: os.stat_result, question: str, model: str) -> tuple[str, str] | None:
        """같은 파일(크기·수정 시각)에 같은 모델이 같은 질문에 한 답."""
        row = self._one("SELECT size, mtime, answer, why FROM answers WHERE key = ? AND question = ? AND model = ?",
                        (self._key(path), question, model))
        if row is None or row[0] != st.st_size or row[1] != st.st_mtime_ns:
            return None
        return row[2], row[3]

    def put_answer(self, path: Path, st: os.stat_result, question: str, model: str, answer: str, why: str) -> None:
        self._write("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self._key(path), question, model, st.st_size, st.st_mtime_ns, answer, why, time.time()))

    def ocr(self, path: Path, st: os.stat_result, model: str) -> dict[int, str]:
        """이 파일에서 글자 인식으로 읽어 둔 쪽들 {쪽(0부터): 글} (파일이 그대로일 때만)."""
        if self.con is None:
            return {}
        try:
            with self._lock:
                rows = self.con.execute("SELECT page, text FROM ocr WHERE key = ? AND model = ? AND size = ? AND mtime = ?",
                                        (self._key(path), model, st.st_size, st.st_mtime_ns)).fetchall()
        except (sqlite3.Error, ValueError):
            return {}
        return {int(r[0]): str(r[1]) for r in rows}

    def put_ocr(self, path: Path, st: os.stat_result, page: int, model: str, text: str) -> None:
        self._write("INSERT OR REPLACE INTO ocr VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self._key(path), page, model, st.st_size, st.st_mtime_ns, text, time.time()))

    def close(self) -> None:
        if self.con is not None:
            try:
                self.con.close()
            except sqlite3.Error:
                pass
            self.con = None


def load(path: Path, st: os.stat_result, deep: bool, cache: DocCache | None) -> tuple[Doc, bool]:
    """(글, 캐시에서 왔는지)."""
    if cache is not None:
        doc = cache.get(path, st, deep)
        if doc is not None:
            return doc, True
    doc = extract(path, deep)
    if cache is not None:
        cache.put(doc, st)
    return doc, False


# ---------------------------------------------------------------------- 보여 주기
def preview(doc: Doc, chars: int) -> str:
    """문서의 첫머리 (한 줄로). 모델이 무슨 문서인지 보는 데 쓴다."""
    text = " ".join(doc.front.split()) or " ".join(doc.body.split())
    return text[:chars] + ("…" if len(text) > chars else "")


def snippet(text: str, needles: list[str], width: int = 60) -> str:
    """처음 맞은 자리 앞뒤 글 (한 줄로)."""
    for needle in needles:
        if not needle:
            continue
        m = re.search(re.escape(needle), text, re.IGNORECASE)
        if m:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            s = " ".join(text[a:b].split())
            return ("…" if a else "") + s + ("…" if b < len(text) else "")
    return ""
