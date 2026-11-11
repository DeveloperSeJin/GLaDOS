"""모델 호출은 이 파일 한 곳에서만 한다 (Ollama /api/chat).

- 요청마다 num_ctx를 적는다 (기본값에 맡기지 않음)
- 출력은 JSON 스키마로 강제 (Ollama `format`)
- 형식 오류는 오류 내용을 붙여 정해진 횟수만 다시 요청, 그다음 FormatError
- 생각 모드(think)는 설정값. 모델이 지원하지 않는다고 하면 빼고 다시 보낸다
- 멈춤 (TUI): stop_event가 있으면 스트리밍으로 받으면서 조각마다 확인하고, 켜지면 연결을 끊고
  Cancelled를 낸다 (Ollama는 연결이 끊기면 생성을 멈춘다). 줄 화면은 Ctrl+C(KeyboardInterrupt)로 멈춘다
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
            "messages": messages,
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

    def close(self) -> None:
        self._http.close()
