"""깨진 글자 시험 (M6.2): 연구원님 PC에서 다운로드 내용 찾기가 통째로 멈춘 일.

PDF에서 뽑은 글에 짝 없는 서로게이트(\\ud82d)가 있었고, 그 글을 캐시(SQLite)에 넣다가 UnicodeEncodeError가 났다.
Windows 파일 이름에도 생길 수 있다. 글이 밖으로 나가는 곳(뽑은 글, 캐시, 도구 결과·오류, 모델 호출)에서 �로 바꾼다.
"""

from __future__ import annotations

import json

import httpx

from glados.config import ModelConfig
from glados.core.llm import OllamaClient
from glados.core.textfix import clean, clean_any
from glados.core.translate import Glossary
from glados.hands import Tool, ToolRegistry, tool_error
from glados.hands import docs
from glados.safety.policy import ToolCall

BROKEN = "penalty \ud82d clause"


def test_clean():
    assert clean(BROKEN) == "penalty \ufffd clause" and clean("정상 😀") == "정상 😀"
    assert clean_any({"a": [BROKEN, 3], "b\udc80": (BROKEN,)}) == {"a": [clean(BROKEN), 3], "b\ufffd": (clean(BROKEN),)}
    clean(BROKEN).encode("utf-8")


def test_extracted_text_and_cache_survive_broken_characters(tmp_path):
    doc = docs.from_text(str(tmp_path / "a.txt"), "앞 " + BROKEN * 400)
    assert "\ud82d" not in doc.text() and "\ufffd" in doc.text()
    cache = docs.DocCache(tmp_path / "doctext.sqlite")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    st = pdf.stat()
    raw = docs.Doc(str(pdf), "ok", front=BROKEN)                    # 고치지 않은 글이 와도 캐시가 멈추지 않는다
    cache.put(raw, st)
    cache.put(docs.Doc(str(tmp_path / "이름\ud82d.pdf"), "ok", front="x"), st)   # 깨진 이름 (Windows)
    assert cache.get(tmp_path / "이름\ud82d.pdf", st, deep=False) is None
    cache.close()


def test_tool_output_and_errors_are_cleaned():
    def bad(args):
        if args.get("fail"):
            raise tool_error("tool.no_file", path="C:\\Users\\User\\Downloads\\a\ud82d.pdf")
        return "- C:\\Users\\User\\Downloads\\a\ud82d.pdf"

    reg = ToolRegistry([Tool("fs.search", "find", run=bad)])
    out = reg.run(ToolCall("fs.search", {}))
    assert "\ufffd" in out and "\ud82d" not in out
    out.encode("utf-8")
    try:
        reg.run(ToolCall("fs.search", {"fail": True}))
    except Exception as e:                                          # ToolError
        assert "\ud82d" not in str(e) and "\ud82d" not in str(e.params)
        str(e).encode("utf-8")
    else:
        raise AssertionError("no error")


def test_model_request_is_cleaned():
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"ok": true}'}, "done": True})

    c = OllamaClient(ModelConfig(host="http://ollama.test", name="qwen3.5:4b", num_ctx=16384, mode="low"),
                     transport=httpx.MockTransport(handler))
    c.chat_json([{"role": "user", "content": BROKEN}], {"type": "object"}, lambda d: d)
    assert sent[-1]["messages"][0]["content"] == clean(BROKEN)


def test_glossary_skips_broken_words(tmp_path):
    asked = []

    class LLM:
        def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
            asked.append(messages[-1]["content"])
            return validate({"words": []}), "{}"

    g = Glossary(LLM(), tmp_path / "names-en.json")
    g.english(["계약\ud82d", "회의록"])
    assert asked and all("\ud82d" not in a for a in asked) and "회의록" in asked[0]
