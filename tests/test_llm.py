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
    assert seen[1]["messages"][-1]["content"].startswith("Format error:")


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


# ---------------------------------------------------------------- 스트리밍과 멈춤 (TUI)
import threading  # noqa: E402

from glados.core.llm import Cancelled, LLMError  # noqa: E402


def stream_lines(*pieces: str) -> bytes:
    lines = [json.dumps({"message": {"role": "assistant", "content": p}, "done": False}, ensure_ascii=False)
             for p in pieces]
    lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def streaming(handler, **kw) -> OllamaClient:
    c = client(handler, **kw)
    c.stop_event = threading.Event()
    return c


def test_stream_is_used_when_stoppable():
    seen = []
    text = json.dumps(GOOD, ensure_ascii=False)

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=stream_lines(text[:7], text[7:30], text[30:]))

    step, raw = chat(streaming(handler))
    assert seen[0]["stream"] is True and json.loads(raw) == GOOD and step.done


def test_stop_before_call_and_while_streaming():
    c = streaming(lambda req: httpx.Response(200, content=stream_lines("{")))
    c.stop_event.set()
    with pytest.raises(Cancelled):
        chat(c)

    ev = threading.Event()
    got = []

    def handler(req):
        def body():
            yield stream_lines('{"thought": ')[:-len(b'{"message": {"role": "assistant", "content": ""}, "done": true}\n')]
            ev.set()                                      # 연구원님이 멈춤
            got.append("second chunk asked")
            yield stream_lines('"x", "done": true}')
        return httpx.Response(200, content=body())

    c = client(handler)
    c.stop_event = ev
    with pytest.raises(Cancelled):
        chat(c)


def test_stream_errors_are_reported():
    c = streaming(lambda req: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ModelMissing):
        chat(c)
    c = streaming(lambda req: httpx.Response(200, content=b'{"error": "out of memory"}\n'))
    with pytest.raises(LLMError) as e:
        chat(c)
    assert e.value.key == "llm.http" and "out of memory" in str(e.value)


def test_stream_think_fallback():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "does not support thinking"})
        return httpx.Response(200, content=stream_lines(json.dumps(GOOD)))

    step, _ = chat(streaming(handler, think=True))
    assert step.done and "think" in seen[0] and "think" not in seen[1] and seen[1]["stream"] is True


def test_cancelled_passes_through_except_exception():
    """도구·번역이 `except Exception`으로 감싸도 멈춤은 지나간다."""
    assert not issubclass(Cancelled, Exception) and issubclass(Cancelled, BaseException)


# ---------------------------------------------------------------- 계량 (TUI가 보여 주는 토큰)
def counted_lines(*pieces: str, prompt: int, output: int) -> bytes:
    lines = [json.dumps({"message": {"role": "assistant", "content": p}, "done": False}, ensure_ascii=False)
             for p in pieces]
    lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                             "prompt_eval_count": prompt, "eval_count": output}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_meter_counts_tokens_and_keeps_the_received_text():
    from glados.core.llm import Meter

    text = json.dumps(GOOD, ensure_ascii=False)
    seen_live = []
    c = streaming(lambda req: httpx.Response(200, content=counted_lines(text[:10], text[10:], prompt=1200, output=48)))
    c.meter = meter = Meter()
    real_piece = meter.piece

    def piece(t):
        real_piece(t)
        seen_live.append(meter.snapshot())        # 받는 동안: 조각 수만큼, 부르는 중

    meter.piece = piece
    chat(c)
    assert [(s[1], s[3]) for s in seen_live] == [(1, True), (2, True)]
    assert seen_live[0][2] == text[:10]
    sent, received, live, active = meter.snapshot()
    assert (sent, received, active) == (1200, 48, False)              # 끝나면 Ollama가 센 값으로
    assert live == text                                               # 받은 글은 다음 호출까지 남는다
    chat(c)
    assert meter.snapshot()[:2] == (2400, 96) and meter.calls == 2    # 호출마다 더한다
    meter.reset()
    assert meter.snapshot() == (0, 0, "", False)


def test_meter_without_counts_and_on_plain_calls():
    from glados.core.llm import Meter

    text = json.dumps(GOOD, ensure_ascii=False)
    c = streaming(lambda req: httpx.Response(200, content=stream_lines(text[:5], text[5:])))
    c.meter = meter = Meter()
    chat(c)
    assert meter.snapshot()[:2] == (0, 2)                             # 셈이 없으면 받은 조각 수
    plain = client(lambda req: httpx.Response(200, json={"message": {"content": text}, "done": True,
                                                         "prompt_eval_count": 50, "eval_count": 9}))
    plain.meter = meter = Meter()
    chat(plain)
    assert meter.snapshot() == (50, 9, text, False)                   # 스트리밍이 아니어도


def test_meter_ends_the_call_when_stopped():
    from glados.core.llm import Meter

    ev = threading.Event()

    def handler(req):
        def body():
            yield stream_lines('{"thought": ')[:-len(b'{"message": {"role": "assistant", "content": ""}, "done": true}\n')]
            ev.set()
            yield stream_lines('"x", "done": true}')
        return httpx.Response(200, content=body())

    c = client(handler)
    c.stop_event = ev
    c.meter = meter = Meter()
    with pytest.raises(Cancelled):
        chat(c)
    sent, received, live, active = meter.snapshot()
    assert not active and received == 1 and live == '{"thought": '
