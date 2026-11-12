"""글자가 없는 쪽(스캔한 PDF)과 그림 파일의 글을 로컬 모델로 읽는다 (M7.3, 글자 인식).

- 쪽을 그림으로 그린다 (pypdfium2, 긴 변 1600픽셀, 흑백). 그림 파일은 크면 줄인다 (Pillow)
- 지금 쓰는 모델(qwen3.5는 그림을 읽을 수 있음)에게 보이는 글을 그대로 옮겨 적게 한다. Ollama라서
  이 컴퓨터 밖으로 나가지 않는다. 따로 OCR 프로그램을 깔지 않는다
- 쪽마다 20초쯤 걸린다: fs.read가 한 번에 몇 쪽만 하고 나머지는 이어서 하라고 알린다
- 옮겨 적은 글은 cache/doctext.sqlite에 둔다 (파일·크기·수정 시각·쪽·모델이 같으면 다시 안 함).
  그 파일이 내용 색인에 있으면 색인에도 넣는다 (다음부터 내용으로 찾을 수 있게)
- 모델이 잘못 읽을 수 있다: 결과에 OCR이라고 적어 이름·숫자를 확인하게 한다
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

MAX_SIDE = 1600
PROMPT = ("Transcribe all text in this image exactly as written, in reading order. Keep the original language; "
          "do not translate, summarize, or describe the image. Separate table cells with |. "
          "If there is no text at all, reply exactly: NO_TEXT")


def can_see(llm: Any) -> bool:
    """이 모델 연결로 그림을 읽을 수 있나 (시험의 가짜 모델에는 없을 수 있다)."""
    return llm is not None and callable(getattr(llm, "see", None))


def _png(img: Any, max_side: int) -> bytes:
    img = img.convert("L")
    w, h = img.size
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_pdf_page(path: Path, index: int, max_side: int = MAX_SIDE) -> bytes:
    """pdf의 한 쪽(0부터)을 PNG로."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[index]
        w, h = page.get_size()
        scale = min(3.0, max_side / max(w, h, 1))
        img = page.render(scale=scale).to_pil()
        return _png(img, max_side)
    finally:
        pdf.close()


def image_png(path: Path, max_side: int = MAX_SIDE) -> bytes:
    from PIL import Image

    with Image.open(str(path)) as img:
        img.seek(0)                                    # 여러 장이면 첫 장 (gif, tiff)
        return _png(img, max_side)


def transcribe(llm: Any, png: bytes) -> str:
    """그림 속 글. 글이 없으면 빈 글."""
    text = str(llm.see(png, PROMPT) or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()     # 코드 칸으로 감싸 보내는 모델
    return "" if text.upper().strip(" .") == "NO_TEXT" else text


# ---------------------------------------------------------------------- 그림이 무엇인지 + 글 (M7.5)
INSPECT_SIDE = 896            # 살펴보기(예·아니요)에는 작게 (빠르게)
BACKGROUND_SIDE = 1280        # 뒤에서 미리 볼 때 (수천 장: 1600보다 빠르고, 신분증 글자는 읽힌다)
DESCRIBE = ("Look at this image. First, one short English sentence saying what it is (for example: a photo of a "
            "Korean driver's license card; a screenshot of a spreadsheet; a photo of a whiteboard; a photo of a cat). "
            "Then transcribe all text in the image exactly as written, in its own language. Reply in this form:\n"
            "DESCRIPTION: <sentence>\nTEXT:\n<the text, or NO_TEXT>")
ASK = ("Question about this image: {question}\nReply in one line: yes, no, or unsure, then \" - \" and the reason "
       "in under 15 English words (what you see that decided it).")
DESCRIBED = "#described"      # 캐시에서 옮겨 적기(모델 이름)와 나눠 두는 표시


def describe(llm: Any, png: bytes) -> str:
    """그림 -> "Image: <무엇인지>\\n<보이는 글>" (색인·읽기에 그대로 쓴다)."""
    raw = str(llm.see(png, DESCRIBE) or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw).strip()
    m = re.search(r"DESCRIPTION:\s*(.*?)\s*(?:\n\s*TEXT:\s*\n?(.*))?$", raw, re.S | re.I)
    if m:
        what, text = m.group(1).strip(), (m.group(2) or "").strip()
    else:                                             # 형식을 안 지킨 답: 통째로 글로
        what, text = "", raw
    if text.upper().strip(" .") == "NO_TEXT":
        text = ""
    head = f"Image: {what}" if what else "Image"
    return head + (f"\n{text}" if text else "")


def describe_cached(llm: Any, path: Path, st: Any, model: str, cache: Any = None, max_side: int = MAX_SIDE) -> str:
    """그림 파일 하나 (캐시: doctext.sqlite의 ocr 표, 쪽 0, 모델 이름 + #described)."""
    key = model + DESCRIBED
    if cache is not None:
        known = cache.ocr(path, st, key)
        if 0 in known:
            return known[0]
    text = describe(llm, image_png(path, max_side))
    if cache is not None:
        cache.put_ocr(path, st, 0, key, text)
    return text


def page_cached(llm: Any, path: Path, st: Any, index: int, model: str, cache: Any = None) -> str:
    """스캔 pdf의 한 쪽 (fs.read와 같은 캐시)."""
    if cache is not None:
        known = cache.ocr(path, st, model)
        if index in known:
            return known[index]
    text = transcribe(llm, render_pdf_page(path, index))
    if cache is not None:
        cache.put_ocr(path, st, index, model, text)
    return text


def ask(llm: Any, png: bytes, question: str) -> tuple[str, str]:
    """그림을 보고 예·아니요 질문에 답한다 (fs.inspect). (yes|no|unsure, 까닭)."""
    raw = " ".join(str(llm.see(png, ASK.format(question=question), max_tokens=60) or "").split())
    m = re.match(r"\W*(yes|no|unsure)\b\W*(.*)", raw, re.I)
    if not m:
        return "unsure", raw[:160] or "no answer"
    return m.group(1).lower(), m.group(2).strip()[:160]
