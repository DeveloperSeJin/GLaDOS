"""모델 호출은 이 파일 한 곳에서만 한다 (Ollama /api/chat).

- 요청마다 num_ctx를 적는다 (기본값에 맡기지 않음)
- 출력은 JSON 스키마로 강제 (Ollama `format`)
- 형식 오류는 오류 내용을 붙여 정해진 횟수만 다시 요청, 그다음 FormatError
- 생각 모드(think)는 설정값. 모델이 지원하지 않는다고 하면 빼고 다시 보낸다
- 멈춤 (TUI): stop_event가 있으면 스트리밍으로 받으면서 조각마다 확인하고, 켜지면 연결을 끊고
  Cancelled를 낸다 (Ollama는 연결이 끊기면 생성을 멈춘다). 줄 화면은 Ctrl+C(KeyboardInterrupt)로 멈춘다
- 임베딩 (M7.2): Embedder가 /api/embed로 글을 벡터로 바꾼다 (뜻으로 찾기). 모델마다 질문·문서 앞에 붙이는 말이 다르다
- 계량 (TUI, M2.7): meter가 있으면 부를 때마다 건넨 토큰(↑, Ollama가 센 prompt_eval_count), 받은 토큰(↓, 받는 동안은
  조각 수, 끝나면 eval_count)과 지금 받고 있는 글을 적는다. 화면이 따로 읽는다 (모델 호출을 기다리게 하지 않음)
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, TypeVar

import httpx
from pydantic import ValidationError

from glados.config import ModelConfig
from glados.core.textfix import clean_any
from glados.i18n import KeyedError

T = TypeVar("T")


class LLMError(KeyedError):
    """모델 호출 실패. 글은 한국어(기록용), 화면은 키로 그 언어."""


class OllamaUnavailable(LLMError):
    """Ollama에 연결할 수 없음."""


class ModelMissing(LLMError):
    """모델이 받아져 있지 않음."""


class FormatError(LLMError):
    """정해진 횟수를 다시 요청해도 형식이 맞지 않음."""


class Cancelled(BaseException):
    """연구원님이 멈춤 (TUI의 Esc·Ctrl+C). KeyboardInterrupt처럼 BaseException이라
    도구·번역의 `except Exception`을 지나 루프까지 올라간다. 루프가 받아 작업을 멈춘다."""


class Meter:
    """작업 하나 동안 모델과 주고받은 것. 모델을 부르는 스레드(작업·도구)가 쓰고, 화면이 따로 읽는다."""

    TAIL = 4000                   # 지금 받고 있는 글은 끝부분만 둔다

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent = 0             # 건넨 토큰 (끝난 호출들)
        self.received = 0         # 받은 토큰 (끝난 호출들)
        self.calls = 0
        self.active = False       # 지금 부르는 중인지
        self._pieces = 0          # 지금 호출에서 받은 조각 수 (조각 하나가 대개 토큰 하나)
        self._live: list[str] = []

    def reset(self) -> None:
        with self._lock:
            self.sent = self.received = self.calls = self._pieces = 0
            self.active = False
            self._live = []

    def begin(self) -> None:
        with self._lock:
            self.calls += 1
            self.active = True
            self._pieces = 0
            self._live = []

    def piece(self, text: str) -> None:
        with self._lock:
            self._pieces += 1
            self._live.append(text)
            if len(self._live) > 512:
                self._live = ["".join(self._live)[-self.TAIL:]]

    def end(self, prompt_tokens: Any = None, output_tokens: Any = None) -> None:
        """호출이 끝남 (실패·멈춤도). 이미 끝난 호출이면 아무것도 하지 않는다. 받은 글은 다음 호출까지 남긴다."""
        with self._lock:
            if not self.active:
                return
            self.sent += _count(prompt_tokens)
            self.received += _count(output_tokens) or self._pieces
            self._pieces = 0
            self.active = False

    def snapshot(self) -> tuple[int, int, str, bool]:
        """(건넨 토큰, 받은 토큰, 지금 받은 글의 끝부분, 부르는 중인지)."""
        with self._lock:
            return self.sent, self.received + self._pieces, "".join(self._live)[-self.TAIL:], self.active


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _short(err: Exception, limit: int = 300) -> str:
    text = str(err).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


class OllamaClient:
    def __init__(
        self,
        cfg: ModelConfig,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 300.0,
    ):
        self.cfg = cfg
        self._think_supported = True
        self.stop_event: threading.Event | None = None    # TUI가 넣는다. 있으면 스트리밍으로 받고 멈출 수 있다
        self.meter: Meter | None = None                    # TUI가 넣는다. 주고받은 토큰과 받고 있는 글
        self._http = httpx.Client(
            base_url=cfg.host,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    # ------------------------------------------------------------------
    def list_models(self) -> list[str]:
        """받아 둔 모델 이름 목록 (연결 확인 겸)."""
        try:
            r = self._http.get("/api/tags")
        except httpx.TransportError as e:
            raise OllamaUnavailable(f"Cannot reach Ollama ({self.cfg.host}): {_short(e)}",
                                    "ollama.unreachable", host=self.cfg.host) from e
        if r.status_code != 200:
            body = _short(Exception(r.text))
            raise LLMError(f"Ollama error {r.status_code}: {body}", "llm.http", status=r.status_code, body=body)
        return [m.get("name", "") for m in r.json().get("models", [])]

    def has_model(self) -> bool:
        names = self.list_models()
        want = self.cfg.name
        return any(n == want or n == f"{want}:latest" or (":" not in want and n.split(":")[0] == want) for n in names)

    # ------------------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        validate: Callable[[dict[str, Any]], T],
        retries: int = 2,
        temperature: float = 0.2,
    ) -> tuple[T, str]:
        """스키마를 강제해 JSON 하나를 받고 검증한다. (검증된 값, 원문)을 돌려준다."""
        msgs = list(messages)
        last_err: Exception | None = None
        for _ in range(retries + 1):
            content = self._chat(msgs, schema, temperature)
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                return validate(data), content
            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                last_err = e
                msgs = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Format error: {_short(e)}. Send exactly one JSON object that matches the schema."},
                ]
        detail = _short(last_err or Exception())
        raise FormatError(f"Malformed output {retries + 1} times in a row: {detail}", "llm.format", count=retries + 1, detail=detail)

    def _chat(self, messages: list[dict[str, str]], schema: dict[str, Any], temperature: float) -> str:
        meter = self.meter
        if meter is not None:
            meter.begin()
        try:
            return self._call(messages, schema, temperature)
        finally:
            if meter is not None:
                meter.end()                              # 실패·멈춤이어도 "부르는 중"을 끝낸다

    def _call(self, messages: list[dict[str, str]], schema: dict[str, Any], temperature: float) -> str:
        body: dict[str, Any] = {
            "model": self.cfg.name,
            "messages": clean_any(messages),            # 깨진 글자가 있으면 JSON을 못 만든다 (M6.2)
            "stream": False,
            "format": schema,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_ctx": self.cfg.num_ctx, "temperature": temperature},
        }
        if self._think_supported:
            body["think"] = self.cfg.think
        stop = self.stop_event
        if stop is not None:
            if stop.is_set():
                raise Cancelled()
            body["stream"] = True
            return self._chat_stream(body, stop, messages, schema, temperature)
        try:
            r = self._http.post("/api/chat", json=body)
        except httpx.TransportError as e:
            raise OllamaUnavailable(f"Cannot reach Ollama ({self.cfg.host}): {_short(e)}",
                                    "ollama.unreachable", host=self.cfg.host) from e

        if r.status_code != 200:
            return self._retry_or_raise(r, messages, schema, temperature)
        try:
            data = r.json()
            content = str(data["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Cannot read the Ollama response: {_short(e)}", "llm.bad_response", detail=_short(e)) from e
        if self.meter is not None:
            self.meter.piece(content)
            self.meter.end(data.get("prompt_eval_count"), data.get("eval_count"))
        return content

    def _chat_stream(self, body: dict[str, Any], stop: threading.Event,
                     messages: list[dict[str, str]], schema: dict[str, Any], temperature: float) -> str:
        """조각으로 받으며 멈춤을 확인한다. 멈추면 with를 빠져나가며 연결을 닫는다 (Ollama가 생성을 멈춤)."""
        parts: list[str] = []
        try:
            with self._http.stream("POST", "/api/chat", json=body) as r:
                if r.status_code != 200:
                    r.read()
                    return self._retry_or_raise(r, messages, schema, temperature)
                for line in r.iter_lines():
                    if stop.is_set():
                        raise Cancelled()
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            detail = _short(Exception(str(chunk["error"])))
                            raise LLMError(f"Ollama error: {detail}", "llm.http", status=200, body=detail)
                        piece = str((chunk.get("message") or {}).get("content") or "")
                    except (AttributeError, TypeError, json.JSONDecodeError) as e:
                        raise LLMError(f"Cannot read the Ollama response: {_short(e)}", "llm.bad_response",
                                       detail=_short(e)) from e
                    parts.append(piece)
                    meter = self.meter
                    if meter is not None and piece:
                        meter.piece(piece)
                    if chunk.get("done"):
                        if meter is not None:
                            meter.end(chunk.get("prompt_eval_count"), chunk.get("eval_count"))
                        break
        except httpx.TransportError as e:
            if stop.is_set():
                raise Cancelled() from e
            raise OllamaUnavailable(f"Cannot reach Ollama ({self.cfg.host}): {_short(e)}",
                                    "ollama.unreachable", host=self.cfg.host) from e
        if stop.is_set():
            raise Cancelled()
        return "".join(parts)

    def _retry_or_raise(self, r: httpx.Response, messages: list[dict[str, str]], schema: dict[str, Any],
                        temperature: float) -> str:
        """200이 아닌 응답. 생각 모드를 모르는 모델이면 think 없이 다시 보낸다."""
        if r.status_code == 404:
            raise ModelMissing(f"Model {self.cfg.name} is not installed. Run 'ollama pull {self.cfg.name}'",
                               "model.missing", model=self.cfg.name)
        if r.status_code == 400 and "think" in r.text.lower() and self._think_supported:
            self._think_supported = False
            return self._call(messages, schema, temperature)
        body = _short(Exception(r.text))
        raise LLMError(f"Ollama error {r.status_code}: {body}", "llm.http", status=r.status_code, body=body)

    def see(self, image: bytes, prompt: str, max_tokens: int = 2048) -> str:
        """그림 하나를 보여 주고 답을 받는다 (글자 인식, M7.3). 형식은 강제하지 않는다 (옮겨 적은 글 그대로)."""
        import base64

        stop = self.stop_event
        if stop is not None and stop.is_set():
            raise Cancelled()
        body: dict[str, Any] = {
            "model": self.cfg.name, "stream": False, "keep_alive": self.cfg.keep_alive,
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode("ascii")]}],
            "options": {"num_ctx": self.cfg.num_ctx, "temperature": 0, "num_predict": max_tokens},
        }
        if self._think_supported:
            body["think"] = False
        meter = self.meter
        if meter is not None:
            meter.begin()
        try:
            r = self._http.post("/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
                self._think_supported = False
                body.pop("think")
                r = self._http.post("/api/chat", json=body)
        except httpx.TransportError as e:
            raise OllamaUnavailable(f"Cannot reach Ollama ({self.cfg.host}): {_short(e)}",
                                    "ollama.unreachable", host=self.cfg.host) from e
        finally:
            if meter is not None:
                meter.end()
        if r.status_code == 404:
            raise ModelMissing(f"Model {self.cfg.name} is not installed. Run 'ollama pull {self.cfg.name}'",
                               "model.missing", model=self.cfg.name)
        if r.status_code != 200:
            body_text = _short(Exception(r.text))
            if any(w in r.text.lower() for w in ("image", "vision", "multimodal")):
                raise LLMError(f"Model {self.cfg.name} cannot read images: {body_text}", "llm.no_vision",
                               model=self.cfg.name)
            raise LLMError(f"Ollama error {r.status_code}: {body_text}", "llm.http", status=r.status_code, body=body_text)
        try:
            data = r.json()
            content = str(data["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Cannot read the Ollama response: {_short(e)}", "llm.bad_response", detail=_short(e)) from e
        if meter is not None:
            meter.end(data.get("prompt_eval_count"), data.get("eval_count"))
        return content

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------- 임베딩 (M7.2, 뜻으로 찾기)
# 모델이 배운 대로: 질문 앞에 붙이는 말, 문서 앞에 붙이는 말 ({text}, {title}). 모델 이름의 ':' 앞으로 고른다
EMBED_FORMATS: dict[str, tuple[str, str]] = {
    "qwen3-embedding": ("Instruct: Given a search query, retrieve documents that are relevant to it\nQuery:{text}",
                        "{text}"),
    "embeddinggemma": ("task: search result | query: {text}", "title: {title} | text: {text}"),
}
EMBED_DIMS = 256            # 벡터 길이 (Matryoshka 모델은 앞부분만 써도 된다. 무시하는 모델은 원래 길이로)


class Embedder:
    """글 -> 벡터 (Ollama /api/embed). 질문과 문서는 모델이 배운 형식으로 따로 감싼다."""

    def __init__(self, host: str, model: str, timeout_s: float = 120.0,
                 transport: httpx.BaseTransport | None = None, keep_alive: str = "10m"):
        self.host = host.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self._http = httpx.Client(base_url=self.host, timeout=httpx.Timeout(timeout_s, connect=5.0), transport=transport)
        family = model.split(":", 1)[0].lower()
        self.query_format, self.doc_format = EMBED_FORMATS.get(family, ("{text}", "{text}"))

    def queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.query_format.format(text=t) for t in texts])

    def documents(self, texts: list[str], title: str = "") -> list[list[float]]:
        return self._embed([self.doc_format.format(text=t, title=title or "none") for t in texts])

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        body = {"model": self.model, "input": clean_any(inputs), "truncate": True, "dimensions": EMBED_DIMS,
                "keep_alive": self.keep_alive}
        try:
            r = self._http.post("/api/embed", json=body)
        except httpx.TransportError as e:
            raise OllamaUnavailable(f"Cannot reach Ollama ({self.host}): {_short(e)}",
                                    "ollama.unreachable", host=self.host) from e
        if r.status_code == 404 or (r.status_code >= 400 and "not found" in r.text.lower()):
            raise ModelMissing(f"Model {self.model} is not installed. Run 'ollama pull {self.model}'",
                               "model.missing", model=self.model)
        if r.status_code >= 400 and "dimensions" in r.text.lower() and "dimensions" in body:
            body.pop("dimensions")                       # 길이를 고를 수 없는 모델: 원래 길이로
            r = self._http.post("/api/embed", json=body)
        if r.status_code != 200:
            detail = _short(Exception(r.text))
            raise LLMError(f"Ollama error {r.status_code}: {detail}", "llm.http", status=r.status_code, body=detail)
        try:
            vectors = r.json()["embeddings"]
            if len(vectors) != len(inputs):
                raise ValueError("wrong number of embeddings")
            return [[float(x) for x in v] for v in vectors]
        except (KeyError, TypeError, ValueError) as e:
            raise LLMError(f"Cannot read the Ollama response: {_short(e)}", "llm.bad_response", detail=_short(e)) from e

    def close(self) -> None:
        self._http.close()
