"""모델 호출 시험 (Ollama를 흉내 내는 가짜 HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest

from glados.config import ModelConfig
from glados.core.llm import FormatError, ModelMissing, OllamaClient, OllamaUnavailable
from glados.core.schema import Step, step_schema

GOOD = {"thought": "안다", "done": True, "answer": {"fact": "가설이 지지되었습니다. p = 0.003.", "wit": "이번에는 우연이 아닙니다."}, "sources": []}


def client(handler, **kw) -> OllamaClient:
    cfg = ModelConfig(host="http://ollama.test", name="qwen3.5:4b", num_ctx=16384, mode="low", **kw)
    return OllamaClient(cfg, transport=httpx.MockTransport(handler))


def reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}, "done": True})


def chat(c: OllamaClient):
    return c.chat_json([{"role": "user", "content": "가설 A 결과?"}], step_schema([]), Step.model_validate)


def test_request_body_has_schema_think_and_num_ctx():
    seen = []

    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        return reply(GOOD)

    step, raw = chat(client(handler))
    body = seen[0]
    assert body["model"] == "qwen3.5:4b" and body["stream"] is False
    assert body["think"] is False and body["options"]["num_ctx"] == 16384
    assert body["format"]["properties"]["done"]["enum"] == [True]
    assert step.answer.fact.startswith("가설이") and json.loads(raw) == GOOD


def test_bad_json_is_retried_with_error_message():
    seen = []
    answers = iter(["이건 JSON이 아닙니다", GOOD])

    def handler(req):
        seen.append(json.loads(req.content))
        return reply(next(answers))

    step, _ = chat(client(handler))
    assert step.done and len(seen) == 2
    assert "형식 오류" in seen[1]["messages"][-1]["content"]


def test_validation_error_is_retried():
    answers = iter([{"thought": "", "done": True}, GOOD])      # done인데 answer 없음
    step, _ = chat(client(lambda req: reply(next(answers))))
    assert step.answer is not None


def test_format_error_after_retries():
    count = []

    def handler(req):
        count.append(1)
        return reply("{broken")

    with pytest.raises(FormatError):
        chat(client(handler))
    assert len(count) == 3           # 처음 1번 + 다시 2번


def test_think_unsupported_falls_back():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "\"qwen3.5:4b\" does not support thinking"})
        return reply(GOOD)

    step, _ = chat(client(handler, think=True))
    assert step.done and "think" in seen[0] and "think" not in seen[1]


def test_connection_error_is_reported():
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(OllamaUnavailable):
        chat(client(handler))


def test_missing_model_is_reported():
    def handler(req):
        return httpx.Response(404, json={"error": "model \"qwen3.5:4b\" not found, try pulling it first"})

    with pytest.raises(ModelMissing) as e:
        chat(client(handler))
    assert "ollama pull qwen3.5:4b" in str(e.value)


def test_has_model():
    tags = {"models": [{"name": "qwen3.5:4b"}, {"name": "qwen3:8b"}]}
    assert client(lambda req: httpx.Response(200, json=tags)).has_model()
    other = {"models": [{"name": "qwen3:8b"}]}
    assert not client(lambda req: httpx.Response(200, json=other)).has_model()
