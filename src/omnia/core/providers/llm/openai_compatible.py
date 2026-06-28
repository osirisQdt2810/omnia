"""OpenAI-compatible LLM provider (OpenAI, OpenRouter, or any compatible endpoint)."""

from __future__ import annotations

import base64
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.llm.base import LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    """Talks to any ``/chat/completions`` + ``/images/generations`` compatible API."""

    name = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        image_model: Optional[str] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        if not api_key:
            raise ProviderError("OpenAI-compatible provider requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._image_model = image_model or "gpt-image-1"
        self._http = http or DEFAULT_HTTP_CLIENT

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        resp = self._http.post_json(
            f"{self._base_url}/chat/completions", payload, headers=self._headers()
        )
        try:
            return str(resp["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected chat response shape: {resp}") from exc

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        payload = {
            "model": self._image_model,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
        }
        resp = self._http.post_json(
            f"{self._base_url}/images/generations", payload, headers=self._headers()
        )
        try:
            return base64.b64decode(resp["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"Unexpected image response shape: {resp}") from exc
