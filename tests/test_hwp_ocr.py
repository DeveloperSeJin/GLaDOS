"""한글 문서와 글자 인식 (M7.3): HWP 5.0·HWPX 읽기, 스캔한 pdf와 그림을 로컬 모델로 옮겨 적기."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest

from conftest import FakeLLM, StaticGlossary
from glados import osinfo
from glados.config import ModelConfig
from glados.core.llm import LLMError, ModelMissing, OllamaClient
from glados.hands import HandContext, ToolError, docs, fs, hwp, ocr
from glados.hands import index as ci
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places
from hwpfake import make_hwp, make_hwpx
from test_fs import make_pdf


# ---------------------------------------------------------------- HWP 5.0
def test_hwp_paragraphs_tables_long_records_and_line_breaks(tmp_path):
    f = tmp_path / "계약.hwp"
    f.write_bytes(make_hwp(["위약금은 계약 금액의 10%", "둘째 문단\n줄 바꿈", "가" * 2100], table_after=0))
    h = hwp.read(f)
    assert h.status == "ok" and h.detail == ""
    assert h.text.startswith("위약금은 계약 금액의 10%\n둘째 문단\n줄 바꿈\n가가")       # 표 자리(8칸)는 건너뜀
    assert h.text.count("가") == 2100                                                  # 4095바이트 넘는 레코드
    plain = tmp_path / "plain.hwp"
    plain.write_bytes(make_hwp(["압축 안 함"], compressed=False))
    assert hwp.read(plain).text == "압축 안 함"


def test_hwp_locked_distribution_old_and_broken(tmp_path):
    locked = tmp_path / "a.hwp"
    locked.write_bytes(make_hwp(["비밀"], props=hwp.PASSWORD))
    assert hwp.read(locked).status == "locked"
    dist = tmp_path / "b.hwp"
    dist.write_bytes(make_hwp(["본문은 암호화"], props=hwp.DISTRIBUTION, preview="미리 보기 글"))
    h = hwp.read(dist)
    assert (h.status, h.text) == ("ok", "미리 보기 글") and "preview only" in h.detail
    old = tmp_path / "c.hwp"
    old.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 100)
    assert hwp.read(old).detail == "HWP 3.0 or older is not supported"
    broken = tmp_path / "d.hwp"
    broken.write_bytes(b"not a document")
    assert hwp.read(broken).status == "error"


def test_para_text_control_characters():
    def u(s: str) -> bytes:
        return s.encode("utf-16-le")

    payload = u("A") + b"\x09\x00" + b"\0" * 14 + u("B") + b"\x18\x00" + u("C") + b"\x1e\x00" + u("D") + b"\x0d\x00"
    assert hwp.para_text(payload) == "A\tB-C D\n"


# ---------------------------------------------------------------- HWPX
def test_hwpx_sections_in_order_line_breaks_and_tables(tmp_path):
    f = tmp_path / "보고서.hwpx"
    sections = [[f"구역 {i}"] for i in range(12)]
    sections[0] = ["첫 문단", "줄\n바꿈", "칸1|칸2"]
    f.write_bytes(make_hwpx(sections))
    h = hwp.read(f)
    assert h.status == "ok"
    assert h.text.startswith("첫 문단\n줄\n바꿈\n칸1\n칸2\n구역 1\n구역 2\n")
    assert h.text.index("구역 2") < h.text.index("구역 10")                           # section10은 section2 뒤
    bad = tmp_path / "bad.hwpx"
    bad.write_bytes(b"PK broken")
    assert hwp.read(bad).status == "error"


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    gh = home / ".glados"
    gh.mkdir()
    places = Places(os=osinfo.current_os(), home=str(home), glados_home=str(gh), resolve_links=False)
    hctx = HandContext(places=places, home=gh, search_default=["~/Documents"], checkpoints=Checkpoints(gh),
                       glossary=StaticGlossary(), model_name="qwen3.5:4b")
    return home, hctx


def test_read_search_and_index_hwp_files(world):
    home, hctx = world
    d = home / "Documents"
    (d / "외주계약.hwp").write_bytes(make_hwp(["제5조 위약금: 계약 금액의 10%"]))
    (d / "회의.hwpx").write_bytes(make_hwpx([["다음 회의: 금요일", "penalty 논의"]]))
    out = fs.read({"path": str(d / "외주계약.hwp")}, hctx)
    assert "제5조 위약금" in out and ", hwp" in out
    assert "다음 회의: 금요일" in fs.read({"path": str(d / "회의.hwpx")}, hctx)
    out = fs.search({"query": "penalty", "content": True}, hctx)                  # 그 자리에서 읽기
    assert "회의.hwpx" in out and "Contents checked: 2 of 2 files" in out
    with pytest.raises(ToolError) as e:
        fs.edit({"path": str(d / "외주계약.hwp"), "old": "a", "new": "b"}, hctx)  # 원본 문서는 고치지 않는다
    assert e.value.key == "tool.edit_doc"
    if ci.fts5_ok():
        idx = ci.FileIndex(hctx.home)
        try:
            ci.run_pass(idx, [str(d)], hctx.places)
            assert idx.stats()["by_status"] == {"ok": 2} and idx.match([["위약금"]])
        finally:
            idx.close()


# ---------------------------------------------------------------- 글자 인식 (OCR)
class Seer(FakeLLM):
    """그림을 받으면 정해 둔 글을 돌려주는 가짜 모델."""

    def __init__(self, texts: list[str] | None = None, fail: Exception | None = None):
        super().__init__([])
        self.texts = list(texts or [])
        self.fail = fail
        self.images: list[bytes] = []

    def see(self, image: bytes, prompt: str, max_tokens: int = 2048) -> str:
        if self.fail:
            raise self.fail
        assert image.startswith(b"\x89PNG") and "transcribe" in prompt.lower()
        self.images.append(image)
        return self.texts.pop(0) if self.texts else "NO_TEXT"


def test_scanned_pdf_pages_are_read_by_ocr_and_cached(world, monkeypatch):
    home, hctx = world
    scan = home / "Documents" / "스캔계약서.pdf"
    scan.write_bytes(make_pdf(["", "", "", "", ""]))
    hctx.llm = Seer(["Article 5. Penalty: 10 percent", "```\nSigned 2025-03-02\n```"])
    out = fs.read({"path": str(scan)}, hctx)
    assert "[p.1, OCR]\nArticle 5. Penalty: 10 percent" in out and "[p.2, OCR]\nSigned 2025-03-02" in out
    assert "scanned pages 1-2 were read by OCR" in out and "pages 3-5 not read yet" in out
    assert len(hctx.llm.images) == 2
    out = fs.read({"path": str(scan)}, hctx)                                        # 두 번째는 캐시에서
    assert "Article 5" in out and len(hctx.llm.images) == 2
    monkeypatch.setattr(fs, "OCR_MAX_PAGES", 2)
    hctx.llm.texts = ["p3", "p4"]
    out = fs.read({"path": str(scan), "pages": "3-5"}, hctx)
    assert "[p.3, OCR]\np3" in out and "[p.4, OCR]\np4" in out and "[p.5]\n(scanned page, not read yet)" in out
    assert "not read yet: pages 5 (OCR takes about 20 s a page" in out


def test_ocr_only_for_pages_without_text_and_errors_are_reported(world):
    home, hctx = world
    mixed = home / "Documents" / "mixed.pdf"
    mixed.write_bytes(make_pdf(["Typed page", ""]))
    hctx.llm = Seer(["Handwritten note"])
    out = fs.read({"path": str(mixed)}, hctx)
    assert "[p.1]\nTyped page" in out and "[p.2, OCR]\nHandwritten note" in out and len(hctx.llm.images) == 1
    other = home / "Documents" / "other.pdf"
    other.write_bytes(make_pdf([""]))
    hctx.llm = Seer(fail=LLMError("Model qwen3.5:4b cannot read images: x", "llm.no_vision", model="qwen3.5:4b"))
    out = fs.read({"path": str(other)}, hctx)
    assert "OCR stopped: Model qwen3.5:4b cannot read images" in out and "(scanned page, not read yet)" in out
    hctx.llm = FakeLLM([])                                                          # 그림을 못 읽는 연결: 예전처럼
    assert "[p.1]\n(no text on this page; it may be a scanned image)" in fs.read({"path": str(other)}, hctx)


@pytest.mark.skipif(not ci.fts5_ok(), reason="no FTS5")
def test_ocr_text_goes_into_the_content_index(world):
    home, hctx = world
    d = home / "Documents"
    scan = d / "scan.pdf"
    scan.write_bytes(make_pdf(["", ""]))
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, [str(d)], hctx.places)
        assert idx.stats()["by_status"] == {"no_text": 1}
    finally:
        idx.close()
    out = fs.search({"query": "indemnity", "content": True}, hctx)
    assert "0 found" in out and "fs.read can read them by OCR" in out
    hctx.llm = Seer(["Indemnity clause", "Page two"])
    fs.read({"path": str(scan)}, hctx)
    out = fs.search({"query": "indemnity", "content": True}, hctx)
    assert "scan.pdf" in out and "(p.1)" in out and "from the content index" in out


def test_images_are_read_by_ocr(world):
    from PIL import Image

    home, hctx = world
    pic = home / "Documents" / "whiteboard.png"
    Image.new("RGB", (4000, 1000), "white").save(str(pic))
    hctx.llm = Seer(["TODO: rerun EXP-03"])
    out = fs.read({"path": str(pic)}, hctx)
    assert "TODO: rerun EXP-03" in out and "image, read by OCR" in out
    assert max(Image.open(io.BytesIO(hctx.llm.images[0])).size) == ocr.MAX_SIDE      # 큰 그림은 줄여서
    assert "TODO" in fs.read({"path": str(pic)}, hctx) and len(hctx.llm.images) == 1   # 캐시
    hctx.llm = FakeLLM([])
    with pytest.raises(ToolError) as e:
        fs.read({"path": str(pic)}, hctx)
    assert e.value.key == "tool.ocr_unavailable"
    bad = home / "Documents" / "bad.jpg"
    bad.write_bytes(b"not an image")
    hctx.llm = Seer()
    with pytest.raises(ToolError) as e:
        fs.read({"path": str(bad)}, hctx)
    assert e.value.key == "tool.image_open"


def test_render_pdf_page_to_png(tmp_path):
    from PIL import Image

    f = tmp_path / "a.pdf"
    f.write_bytes(make_pdf(["Hello"]))
    png = ocr.render_pdf_page(f, 0)
    img = Image.open(io.BytesIO(png))
    assert png.startswith(b"\x89PNG") and img.size == (900, 432) and img.mode == "L"      # 작은 쪽은 3배까지


def test_ollama_see_sends_the_image_and_reports_models_without_vision():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == "text-only":
            return httpx.Response(500, json={"error": "this model does not support images"})
        if body["model"] == "gone":
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"message": {"content": "Hello"}, "prompt_eval_count": 900, "eval_count": 3})

    t = httpx.MockTransport(handler)
    client = OllamaClient(ModelConfig(name="qwen3.5:4b", num_ctx=16384), transport=t)
    assert client.see(b"\x89PNGdata", "Transcribe") == "Hello"
    msg = seen[-1]["messages"][0]
    assert base64.b64decode(msg["images"][0]) == b"\x89PNGdata" and msg["content"] == "Transcribe"
    assert seen[-1]["think"] is False and "format" not in seen[-1] and seen[-1]["options"]["num_ctx"] == 16384
    with pytest.raises(LLMError) as e:
        OllamaClient(ModelConfig(name="text-only"), transport=t).see(b"x", "p")
    assert e.value.key == "llm.no_vision"
    with pytest.raises(ModelMissing):
        OllamaClient(ModelConfig(name="gone"), transport=t).see(b"x", "p")


def test_docs_extract_and_full_text_read_hwp(tmp_path):
    f = tmp_path / "a.hwpx"
    f.write_bytes(make_hwpx([["제목 줄", "본문 penalty"]]))
    doc = docs.extract(f, deep=True)
    assert doc.status == "ok" and "본문 penalty" in doc.text()
    ft = docs.full_text(f)
    assert ft.status == "ok" and ft.text == "제목 줄\n본문 penalty"
