"""내용 색인 (M7.1): FTS5 trigram 색인, 훑기, fs.search가 색인을 먼저 쓰는지, 색인 프로세스, /index."""

from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest
from docx import Document

from conftest import FakeLLM, StaticGlossary
from glados import cli, osinfo
from glados.config import ensure_home, load_config
from glados.hands import HandContext, docs
from glados.hands import fs
from glados.hands import index as ci
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places
from test_fs import make_pdf

pytestmark = pytest.mark.skipif(not ci.fts5_ok(), reason="this Python's SQLite has no FTS5 trigram")


def put_text(idx: ci.FileIndex, path, text: str, status: str = "ok", pages: int = 0) -> int:
    path.write_text(text, encoding="utf-8")
    return idx.put(path, path.stat(), status, pages, pages, "", text)


@pytest.fixture
def world(tmp_path):
    """가짜 홈: 다운로드·문서와 숨은 폴더, 건너뛸 폴더, GLaDOS 설정 폴더(안전장치)."""
    home = tmp_path / "home"
    dl, documents = home / "Downloads", home / "Documents"
    (dl / "계약").mkdir(parents=True)
    documents.mkdir()
    gh = home / "glados-home"                                   # 숨은 폴더가 아니어도 안전장치라 읽지 않는다
    gh.mkdir()
    (dl / "계약" / "2025_외주개발_계약서.pdf").write_bytes(make_pdf(["Term: 12 months", "Penalty clause: 10 percent"]))
    (dl / "2024_임대_계약서.pdf").write_bytes(make_pdf(["Lease"]))
    (documents / "회의록.md").write_text("# 회의록\n- 할 일: 실험 3 재현, 논문 초안\n", encoding="utf-8")
    (documents / "예전메모.txt").write_bytes("한글 메모입니다. penalty 없음".encode("cp949"))
    d = Document()
    d.add_paragraph("Quarterly report on protein folding")
    d.save(str(documents / "report.docx"))
    (documents / ".hidden").mkdir()
    (documents / ".hidden" / "secret.txt").write_text("penalty hidden", encoding="utf-8")
    (documents / "node_modules").mkdir()
    (documents / "node_modules" / "x.md").write_text("penalty in node_modules", encoding="utf-8")
    (gh / "notes.md").write_text("penalty in GLaDOS home", encoding="utf-8")
    places = Places(os=osinfo.current_os(), home=str(home), glados_home=str(gh), resolve_links=False)
    hctx = HandContext(places=places, home=gh, search_default=["~/Downloads", "~/Documents"],
                       checkpoints=Checkpoints(gh), glossary=StaticGlossary())
    roots = [str(dl), str(documents), str(gh)]
    return home, hctx, roots


# ---------------------------------------------------------------- 색인 자체
def test_match_long_and_short_words_pages_and_snippets(tmp_path):
    idx = ci.FileIndex(tmp_path / "gh")
    try:
        a = put_text(idx, tmp_path / "a.txt", "page one: term\fPage two: PENALTY clause, 10 percent", pages=2)
        b = put_text(idx, tmp_path / "b.txt", "학위논문 초안과 모델 비교")
        c = put_text(idx, tmp_path / "c.txt", "위약금은 계약 금액의 10%")
        assert idx.match([["penalty"]]) == [a]                                   # 대소문자 무시
        assert idx.match([["논문"]]) == [b]                                      # 두 글자: 글에서 직접 (LIKE)
        assert sorted(idx.match([["위약금", "penalty"]])) == sorted([a, c])       # 묶음 안은 '또는'
        assert idx.match([["penalty"], ["percent"]]) == [a]                      # 묶음끼리는 '그리고'
        assert idx.match([["penalty"], ["모델"]]) == []
        assert idx.match([["10%"]]) == [c] and idx.match([["50_%"]]) == []       # LIKE 특수 글자는 글자 그대로
        snip, page = idx.snippet(a, ["penalty"])
        assert page == 2 and "PENALTY clause" in snip
        assert idx.snippet(b, ["논문"])[1] == 0                                  # pdf가 아니면 쪽 번호 없음
        st = idx.stats()
        assert st["files"] == 3 and st["by_status"] == {"ok": 3}
    finally:
        idx.close()


def test_one_indexer_at_a_time(tmp_path):
    idx = ci.FileIndex(tmp_path)
    try:
        assert idx.claim(100, now=1000.0)
        assert not idx.claim(200, now=1010.0)                                    # 다른 것이 살아 있다
        assert idx.claim(100, now=1020.0)                                        # 자기 자신은 다시
        assert idx.claim(200, now=1020.0 + ci.STALE_S + 1)                        # 소식이 끊기면 넘겨받는다
    finally:
        idx.close()


def test_pass_reads_new_and_changed_files_and_forgets_gone_ones(world):
    home, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        first = ci.run_pass(idx, roots, hctx.places)
        names = sorted(os.path.basename(e) for e in idx.all_ids())
        assert names == sorted(["2025_외주개발_계약서.pdf", "2024_임대_계약서.pdf", "회의록.md", "예전메모.txt",
                                "report.docx"])                               # 숨은 폴더·node_modules·안전장치는 없다
        assert first == {"seen": 5, "read": 5}
        pdf = home / "Downloads" / "계약" / "2025_외주개발_계약서.pdf"
        e = idx.entries([ci.file_key(pdf)])[ci.file_key(pdf)]
        assert (e.status, e.pages, e.read_pages) == ("ok", 2, 2) and e.fresh(pdf.stat())
        assert idx.snippet(e.id, ["penalty"])[1] == 2                            # 2쪽
        assert idx.match([["protein"]]) and idx.match([["메모입니다"]])            # docx, cp949 글 파일

        assert ci.run_pass(idx, roots, hctx.places) == {"seen": 5, "read": 0}    # 그대로면 다시 읽지 않는다
        memo = home / "Documents" / "회의록.md"
        memo.write_text("# 회의록\n- 새 안건: 데이터 증강\n", encoding="utf-8")
        os.utime(memo, (time.time() + 5, time.time() + 5))
        (home / "Downloads" / "2024_임대_계약서.pdf").unlink()
        assert ci.run_pass(idx, roots, hctx.places) == {"seen": 4, "read": 1}
        assert len(idx.all_ids()) == 4 and idx.match([["데이터 증강"]]) and not idx.match([["실험 3"]])
        assert idx.stats()["state"] == "idle"
    finally:
        idx.close()


def test_pass_stops_when_asked(world):
    _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    stop = ci.threading.Event()
    stop.set()
    try:
        assert ci.run_pass(idx, roots, hctx.places, stop) == {"seen": 5, "read": 0}
        assert idx.stats()["state"] == "reading"                                # 끝까지 훑지 않았으니 지우지도 않는다
    finally:
        idx.close()


# ---------------------------------------------------------------- 찾기가 색인을 쓴다
def index_world(world):
    _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, roots, hctx.places)
    finally:
        idx.close()


def test_content_search_uses_the_index_without_opening_files(world, monkeypatch):
    home, hctx, _ = world
    index_world(world)

    def no(*a, **k):
        raise AssertionError("indexed files must not be opened")

    monkeypatch.setattr(docs, "load", no)
    monkeypatch.setattr(fs, "_decode", no)
    out = fs.search({"query": "penalty", "content": True}, hctx)
    assert "2025_외주개발_계약서.pdf" in out and '· in the text (p.2): "' in out
    assert "예전메모.txt" in out and "secret.txt" not in out and "node_modules" not in out
    assert "Contents checked: 5 of 5 files (from the content index: whole text)." in out
    out = fs.search({"query": "논문", "content": True}, hctx)                     # 두 글자 한국어 낱말
    assert "회의록.md" in out


def test_changed_files_are_read_again_on_the_spot(world):
    home, hctx, _ = world
    index_world(world)
    memo = home / "Documents" / "회의록.md"
    memo.write_text("# 회의록\n- 새 안건: augmentation\n", encoding="utf-8")
    os.utime(memo, (time.time() + 5, time.time() + 5))
    out = fs.search({"query": "augmentation", "content": True}, hctx)
    assert "회의록.md" in out
    assert "4 from the content index (whole text), the rest read now" in out


def test_name_search_mentions_matches_in_the_text(world):
    _, hctx, _ = world
    out = fs.search({"query": "protein"}, hctx)
    assert "content index" not in out                                           # 색인이 없으면 말하지 않는다
    index_world(world)
    out = fs.search({"query": "protein"}, hctx)
    assert "The text of 1 file contains these words (content index); search again with content=true" in out


def test_without_fts5_search_reads_as_before(world, monkeypatch):
    _, hctx, _ = world
    index_world(world)
    monkeypatch.setattr(ci, "fts5_ok", lambda: False)
    out = fs.search({"query": "penalty", "content": True}, hctx)
    assert "2025_외주개발_계약서.pdf" in out and "content index" not in out


# ---------------------------------------------------------------- 색인 프로세스와 /index
def test_index_process_runs_and_ends_with_glados(world):
    _, hctx, roots = world
    proc = ci.IndexProcess(hctx.home, roots, hctx.places)
    try:
        busy = hctx.home / "cache" / ci.BUSY_FILE
        proc.busy(True)
        assert busy.exists()
        proc.busy(False)
        assert not busy.exists()
        deadline = time.time() + 60
        state = ""
        while time.time() < deadline:
            idx = ci.open_index(hctx.home)
            if idx is not None:
                try:
                    state = idx.meta("state")
                finally:
                    idx.close()
            if state == "idle":
                break
            time.sleep(0.3)
        assert state == "idle" and proc.alive()
    finally:
        proc.stop(wait_s=10)
    assert not proc.alive()
    idx = ci.open_index(hctx.home)
    try:
        assert idx.stats()["files"] == 5 and idx.meta("state") == "stopped" and not idx.running()
    finally:
        idx.close()


def open_app(tmp_path, enabled: bool = True) -> tuple[cli.App, io.StringIO]:
    home = tmp_path / "gh"
    ensure_home(home)
    if not enabled:
        text = (home / "glados.toml").read_text(encoding="utf-8").replace("enabled = true", "enabled = false")
        (home / "glados.toml").write_text(text, encoding="utf-8")
    out = io.StringIO()
    return cli.App(load_config(home), cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux"), out


def test_index_command_shows_the_state(tmp_path):
    app, out = open_app(tmp_path)
    try:
        assert app.index_lines() == [cli.t("index.none")]
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "a.md").write_text("penalty", encoding="utf-8")
        (docs_dir / "scan.pdf").write_bytes(make_pdf([""]))
        idx = ci.FileIndex(app.cfg.home)
        try:
            ci.run_pass(idx, [str(docs_dir)], app.places)
            idx.set_meta(roots='["' + str(docs_dir).replace("\\", "\\\\") + '"]')
        finally:
            idx.close()
        app.command("/index")
        text = out.getvalue()
        assert "파일 2개 (글 1 · 스캔 PDF 1 · 암호 0 · 못 읽음 0)" in text
        assert "지금은 색인 프로세스가 돌지 않습니다" in text and "마지막으로 훑은 때" in text
        assert f"폴더: {docs_dir}" in text and "index.sqlite" in text
    finally:
        app.close()
    off, _ = open_app(tmp_path / "off", enabled=False)
    try:
        assert off.index_lines() == [cli.t("index.off")]
        assert off.cfg.index.enabled is False
        off.start_background()
        assert off.indexer is None
    finally:
        off.close()


def test_app_starts_and_stops_the_indexer_and_marks_tasks(tmp_path, monkeypatch):
    started: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd, **kw):
            started.append(cmd)
            self.stdin = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    real = ci.IndexProcess
    monkeypatch.setattr(ci, "IndexProcess", lambda home, roots, places, **kw: real(home, roots, places, popen=FakeProc, **kw))
    app, _ = open_app(tmp_path)
    try:
        (tmp_path / "Downloads").mkdir(exist_ok=True)
        app.places = app.hctx.places = ci_places = Places(os=osinfo.current_os(), home=str(tmp_path),
                                                          glados_home=str(app.cfg.home))
        app.hctx.user_dirs = {}
        app.start_background()
        assert app.indexer is not None and started[0][:3] == [sys.executable, "-m", "glados.hands.index"]
        job = json.loads((app.cfg.home / "cache" / ci.JOB_FILE).read_text(encoding="utf-8"))
        assert job["roots"] == [str(tmp_path / "Downloads")] and job["places"]["home"] == ci_places.home
        assert (job["host"], job["embed_model"]) == ("http://localhost:11434", "qwen3-embedding:0.6b")
        app.llm.steps = [{"thought": "Answer now.", "done": True, "answer": {"fact": "Hi.", "wit": ""}, "sources": []}]
        seen = []
        real_busy = app.indexer.busy
        app.indexer.busy = lambda on: (seen.append(on), real_busy(on))
        app.ask_task("hello")
        assert seen == [True, False]
    finally:
        app.close()
    assert app.indexer is None


# ---------------------------------------------------------------- 뜻으로 찾기 (M7.2)
CONCEPTS = {"penalty": ("penalty", "위약금", "fine", "breach"), "protein": ("protein", "folding"),
            "lease": ("lease", "임대", "rent")}


class FakeEmbedder:
    """낱말 묶음(개념)이 들어 있으면 그 칸이 1인 벡터. 질문과 문서가 같은 칸이면 닮았다."""

    model = "fake-embed"

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.docs: list[tuple[list[str], str]] = []

    def vec(self, text: str) -> list[float]:
        low = text.lower()
        return [1.0 if any(w in low for w in words) else 0.0 for words in CONCEPTS.values()] + [0.05]

    def documents(self, texts, title=""):
        if self.fail:
            raise self.fail
        self.docs.append((list(texts), title))
        return [self.vec(t) for t in texts]

    def queries(self, texts):
        if self.fail:
            raise self.fail
        return [self.vec(t) for t in texts]


def test_chunks_cover_the_start_and_the_whole_document():
    text = "".join(f"[{i:03d}]" + "x" * 95 for i in range(100))            # 10,000자 = 1000자 조각 10개
    pieces = ci.chunks(text, size=1000, most=4)
    assert [start for start, _ in pieces] == [0, 3000, 6000, 9000] and pieces[0][1].startswith("[000]")
    assert ci.chunks("page one\fpage two", size=1000)[0][1] == "page one page two"
    assert ci.chunks("   ") == []


def test_embed_pass_makes_vectors_waits_while_busy_and_redoes_changed_files(world):
    _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, roots, hctx.places)
        emb = FakeEmbedder()
        busy = iter([True, False] + [False] * 100)
        waits: list[float] = []
        stop = ci.threading.Event()
        stop.wait = lambda s: waits.append(s) or False                          # 쉬는 동안 기다리지 않고 센다
        assert ci.embed_pass(idx, emb, lambda: next(busy), stop) == 5
        assert waits == [ci.BUSY_WAIT_S] and idx.stats()["embed_state"] == "idle"
        assert idx.stats()["vectors"] == {"fake-embed": 5}
        assert any(title == "none" or title == "" for _, title in emb.docs)
        assert ci.embed_pass(idx, emb, lambda: False) == 0                    # 다 했으면 다시 하지 않는다
        memo = world[0] / "Documents" / "회의록.md"
        memo.write_text("임대 계약 정리", encoding="utf-8")
        os.utime(memo, (time.time() + 5, time.time() + 5))
        ci.run_pass(idx, roots, hctx.places)
        assert idx.stats()["vectors"] == {"fake-embed": 4, "": 1}              # 글이 바뀌면 벡터도 다시
        assert ci.embed_pass(idx, emb, lambda: False) == 1
    finally:
        idx.close()


def test_embed_pass_reports_a_missing_model(world):
    from glados.core.llm import ModelMissing

    _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, roots, hctx.places)
        missing = ModelMissing("Model fake is not installed", "model.missing", model="fake")
        assert ci.embed_pass(idx, FakeEmbedder(fail=missing), lambda: False) == 0
        assert idx.meta("embed_state") == "missing"
    finally:
        idx.close()


def test_search_by_meaning_finds_files_with_other_words(world):
    _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, roots, hctx.places)
        hctx.embedder = FakeEmbedder()
        out = fs.search({"query": "breach", "content": True}, hctx)
        assert "Search by meaning: not ready yet" in out                       # 벡터를 아직 안 만들었다
        ci.embed_pass(idx, hctx.embedder, lambda: False)
    finally:
        idx.close()
    out = fs.search({"query": "breach", "content": True}, hctx)                # 글에 "breach"는 없다
    assert "0 found" in out and "Close in meaning (the words may differ; read before relying on them):" in out
    lines = [ln for ln in out.splitlines() if "· meaning" in ln]
    assert len(lines) == 2 and any("2025_외주개발_계약서.pdf" in ln and "(p.1)" in ln for ln in lines)
    assert any("예전메모.txt" in ln for ln in lines) and not any("report.docx" in ln for ln in lines)   # 먼 것은 빼고
    out = fs.search({"query": "penalty", "content": True}, hctx)               # 낱말로 찾은 파일은 다시 보이지 않는다
    assert "Close in meaning" not in out and out.count("2025_외주개발_계약서.pdf") == 1
    hctx.embedder = FakeEmbedder(fail=ci_llm_error())
    out = fs.search({"query": "breach", "content": True}, hctx)
    assert "Search by meaning is unavailable: Cannot reach Ollama" in out


def ci_llm_error():
    from glados.core.llm import OllamaUnavailable

    return OllamaUnavailable("Cannot reach Ollama (http://localhost:11434): refused", "ollama.unreachable",
                             host="http://localhost:11434")


def test_index_made_before_meaning_search_gets_the_new_column(tmp_path):
    import sqlite3

    path = ci.index_path(tmp_path)
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, key TEXT UNIQUE, path TEXT, size INTEGER, mtime INTEGER, "
                "version INTEGER, status TEXT, pages INTEGER, read_pages INTEGER, title TEXT, chars INTEGER, indexed REAL)")
    con.commit()
    con.close()
    idx = ci.FileIndex(tmp_path)
    try:
        put_text(idx, tmp_path / "a.txt", "penalty")
        assert idx.needs_vectors("m") and idx.stats()["vectors"] == {"": 1}
    finally:
        idx.close()


def test_embedder_formats_and_errors():
    import httpx

    from glados.core.llm import Embedder, ModelMissing

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == "nope:1b":
            return httpx.Response(404, json={"error": "model 'nope:1b' not found"})
        if body["model"] == "old:1b" and "dimensions" in body:
            return httpx.Response(400, json={"error": "this model does not support dimensions"})
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2] for _ in body["input"]]})

    t = httpx.MockTransport(handler)
    q = Embedder("http://x", "qwen3-embedding:0.6b", transport=t)
    assert q.queries(["penalty clause"]) == [[0.1, 0.2]]
    assert seen[-1]["input"][0].startswith("Instruct: ") and seen[-1]["input"][0].endswith("\nQuery:penalty clause")
    assert seen[-1]["dimensions"] == 256 and seen[-1]["truncate"] is True
    q.documents(["text"], "Title")
    assert seen[-1]["input"] == ["text"]
    g = Embedder("http://x", "embeddinggemma:300m", transport=t)
    g.documents(["body"])
    assert seen[-1]["input"] == ["title: none | text: body"]
    g.queries(["q"])
    assert seen[-1]["input"] == ["task: search result | query: q"]
    old = Embedder("http://x", "old:1b", transport=t)
    assert old.documents(["a"]) == [[0.1, 0.2]] and "dimensions" not in seen[-1]   # 길이를 못 고르는 모델
    with pytest.raises(ModelMissing):
        Embedder("http://x", "nope:1b", transport=t).queries(["a"])


def test_index_command_shows_search_by_meaning(tmp_path):
    app, out = open_app(tmp_path)
    try:
        st = {"vectors": {"qwen3-embedding:0.6b": 3, "": 2}, "embed_state": "waiting"}
        assert app.meaning_lines(st) == [cli.t("index.meaning_ready", ready=3, total=5, model="qwen3-embedding:0.6b"),
                                         cli.t("index.meaning_waiting")]
        assert app.meaning_lines({"vectors": {"": 5}, "embed_state": "missing"}) == [
            cli.t("index.meaning_missing", model="qwen3-embedding:0.6b")]
        assert "ollama pull qwen3-embedding:0.6b" in cli.t("index.meaning_missing", model="qwen3-embedding:0.6b")
        app.cfg.index.embed_model = ""
        assert app.meaning_lines(st) == [cli.t("index.meaning_off")]
        assert app.hctx.embedder is not None and app.hctx.embedder.model == "qwen3-embedding:0.6b"
    finally:
        app.close()
