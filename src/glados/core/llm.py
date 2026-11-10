"""모델 호출은 이 파일 한 곳에서만 한다 (Ollama /api/chat).

- 요청마다 num_ctx를 적는다 (기본값에 맡기지 않음)
- 출력은 JSON 스키마로 강제 (Ollama `format`)
- 형식 오류는 오류 내용을 붙여 정해진 횟수만 다시 요청, 그다음 FormatError
- 생각 모드(think)는 설정값. 모델이 지원하지 않는다고 하면 빼고 다시 보낸다
"""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

import httpx
from pydantic import ValidationError

from glados.config import ModelConfig

T = TypeVar("T")


class LLMError(Exception):
    """모델 호출 실패."""


class OllamaUnavailable(LLMError):
    """Ollama에 연결할 수 없음."""


class ModelMissing(LLMError):
    """모델이 받아져 있지 않음."""


class FormatError(LLMError):
    """정해진 횟수를 다시 요청해도 형식이 맞지 않음."""


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
            raise OllamaUnavailable(f"Ollama에 연결할 수 없습니다 ({self.cfg.host}): {_short(e)}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama 응답 오류 {r.status_code}: {_short(Exception(r.text))}")
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
                    raise ValueError("JSON 객체가 아닙니다")
                return validate(data), content
            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                last_err = e
                msgs = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"형식 오류: {_short(e)}. 스키마에 맞는 JSON 하나만 다시 보내세요."},
                ]
        raise FormatError(f"형식 오류가 {retries + 1}번 이어졌습니다: {_short(last_err or Exception())}")

    def _chat(self, messages: list[dict[str, str]], schema: dict[str, Any], temperature: float) -> str:
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
        try:
            r = self._http.post("/api/chat", json=body)
        except httpx.TransportError as e:
            raise OllamaUnavailable(f"Ollama에 연결할 수 없습니다 ({self.cfg.host}): {_short(e)}") from e

        if r.status_code == 404:
            raise ModelMissing(f"모델 {self.cfg.name}이(가) 없습니다. 'ollama pull {self.cfg.name}'으로 받으세요")
        if r.status_code == 400 and "think" in r.text.lower() and self._think_supported:
            # 생각 모드를 모르는 모델: think 없이 다시 보낸다
            self._think_supported = False
            return self._chat(messages, schema, temperature)
        if r.status_code != 200:
            raise LLMError(f"Ollama 응답 오류 {r.status_code}: {_short(Exception(r.text))}")
        try:
            return str(r.json()["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama 응답을 읽을 수 없습니다: {_short(e)}") from e

    def close(self) -> None:
        self._http.close()
