"""OpenAI-compatible LLM provider (OpenAI, OpenRouter, or any compatible endpoint)."""

from __future__ import annotations

import base64
from typing import Optional

from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import LLMProvider


def _usage_from_openai(resp: object) -> Optional[dict[str, int]]:
    """Extract token usage from an OpenAI-compatible chat response (None if absent)."""
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "in": int(usage.get("prompt_tokens", 0)),
        "out": int(usage.get("completion_tokens", 0)),
        "total": int(usage.get("total_tokens", 0)),
    }


class OpenAICompatibleProvider(LLMProvider):
    """Talks to any ``/chat/completions`` + ``/images/generations`` compatible API."""

    name = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        image_model: Optional[str] = None,
        temperature: float = 0.7,
        http: Optional[HttpClient] = None,
    ) -> None:
        if not api_key:
            raise ProviderError("OpenAI-compatible provider requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._image_model = image_model or "gpt-image-1"
        self._temperature = temperature
        self._http = http or DEFAULT_HTTP_CLIENT

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        resp = self._http.post_json(
            f"{self._base_url}/chat/completions", payload, headers=self._headers()
        )
        self.last_usage = _usage_from_openai(resp)
        try:
            return str(resp["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected chat response shape: {resp}") from exc

    def fetch_credit(self) -> Optional[dict]:
        """Return OpenRouter credit ``{total, used, remaining}``, else None (best-effort).

        Only meaningful for an OpenRouter endpoint: GET ``<base_url>/credits`` with the api
        key and parse OpenRouter's ``data.total_credits`` / ``data.total_usage``. Returns None
        for a non-OpenRouter base URL or on any error (never raises) — the Account dialog
        treats a missing credit line as "unknown".
        """
        if "openrouter" not in self._base_url:
            return None
        try:
            resp = self._http.get_json(
                f"{self._base_url}/credits", headers=self._headers()
            )
            data = resp.get("data", {})
            total = float(data["total_credits"])
            used = float(data["total_usage"])
        except (ProviderError, KeyError, TypeError, ValueError):
            return None
        return {"total": total, "used": used, "remaining": total - used}

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
