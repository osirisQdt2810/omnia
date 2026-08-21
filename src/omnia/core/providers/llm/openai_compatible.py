"""OpenAI-compatible LLM provider (OpenAI, OpenRouter, or any compatible endpoint)."""

from __future__ import annotations

import base64
from typing import Any, Optional

from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import LLMProvider, PromptParts
from omnia.core.providers.llm.registry import register_llm
from omnia.core.providers.openai_family import openai_family_base_url

# Default base URL per config name — the openai family is ONE class under three names that
# differ only by where they point. ``from_config`` picks the URL by ``config['provider']``.


def _usage_from_openai(resp: object) -> Optional[dict[str, int]]:
    """Extract token usage from an OpenAI-compatible chat response (None if absent).

    ``prompt_tokens_details.cached_tokens`` — the part of ``prompt_tokens`` the endpoint served
    from its prompt cache — is reported under ``"cached"``, and only when non-zero (matching
    Gemini's shape, and keeping the dict unchanged for an endpoint that reports no cache at
    all). It is what makes prompt caching measurable rather than assumed.
    """
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {
        "in": int(usage.get("prompt_tokens", 0)),
        "out": int(usage.get("completion_tokens", 0)),
        "total": int(usage.get("total_tokens", 0)),
    }
    details = usage.get("prompt_tokens_details")
    cached = (
        int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    )
    if cached:
        counts["cached"] = cached
    return counts


@register_llm("openai", "openrouter", "openai_compatible")
class OpenAICompatibleProvider(LLMProvider):
    """Talks to any ``/chat/completions`` + ``/images/generations`` compatible API."""

    name = "openai_compatible"
    # OpenAI-hosted models cache a repeated prompt prefix automatically above a token threshold,
    # with nothing extra on the wire — so the inherited concatenating ``generate_cached_text``
    # already benefits and only ``cached_tokens`` needs reporting. Anthropic-family models via
    # OpenRouter instead need an explicit marker, which is what ``prompt_cache_control`` turns
    # on. A self-hosted endpoint may do neither; then the request is byte-for-byte today's.
    supports_prompt_cache = True
    # Declared True because the CLASS can send ``response_format``; whether a given endpoint
    # honours it is per MODEL (an OpenRouter deployment may ignore or reject it), so the wire
    # change itself is behind ``[llm.<name>].json_output``. The flag advertises the capability;
    # the setting decides whether to use it. See ``generate_json``.
    supports_json_output = True

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        image_model: Optional[str] = None,
        temperature: float = 0.7,
        http: Optional[HttpClient] = None,
        prompt_cache_control: bool = False,
        json_output: bool = False,
    ) -> None:
        if not api_key:
            raise ProviderError("OpenAI-compatible provider requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._image_model = image_model or "gpt-image-1"
        self._temperature = temperature
        self._http = http or DEFAULT_HTTP_CLIENT
        self._prompt_cache_control = prompt_cache_control
        self._json_output = json_output

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> OpenAICompatibleProvider:
        """Build the provider for whichever openai-family name ``config`` selects.

        Args:
            config: The provider's config subsection (``provider`` picks the default base URL).
            http: Optional HTTP client to inject.

        Returns:
            The configured provider.
        """
        # This class serves three config names, so the whole config goes to the resolver: the
        # name inside it is the only thing that distinguishes an openrouter build from an
        # openai one.
        return cls(
            api_key=config.get("api_key", ""),
            base_url=openai_family_base_url(config),
            model=config.get("model", "gpt-4o-mini"),
            image_model=config.get("image_model"),
            temperature=float(config.get("temperature", 0.7)),
            http=http,
            prompt_cache_control=bool(config.get("prompt_cache_control", False)),
            json_output=bool(config.get("json_output", False)),
        )

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
        text, _usage = self.generate_text_with_usage(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        return text

    def _chat(
        self,
        content: object,
        *,
        system: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """POST one ``/chat/completions`` whose user message carries ``content``.

        ``content`` is a plain string for an ordinary call, or the content-parts list the
        cache-control path needs; ``response_format`` is the JSON-mode envelope, sent only when
        the caller asked for it. Shared so the paths can only differ in those two values — the
        model, temperature, token cap, headers and parsing are literally the same code.
        """
        # ``object`` values, not ``str``: the cache-control path puts a content-parts LIST here.
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        resp = self._http.post_json(
            f"{self._base_url}/chat/completions", payload, headers=self._headers()
        )
        # Return the usage parsed from THIS response; also set last_usage for external readers.
        usage = _usage_from_openai(resp)
        self.last_usage = usage
        try:
            return str(resp["choices"][0]["message"]["content"]), usage
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected chat response shape: {resp}") from exc

    def generate_text_with_usage(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        return self._chat(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )

    def generate_cached_text(
        self,
        parts: PromptParts,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Mark ``parts.prefix`` cacheable, when the endpoint was configured to accept that.

        Anthropic-family models via OpenRouter only cache a prefix that carries an explicit
        ``cache_control`` marker, and a marker can only sit on a content PART — so opting in
        means sending ``content`` as a two-element array instead of a string. That is a wire
        format change and support for it is per MODEL, not per provider: an endpoint that
        rejects the array fails generation outright. Hence the opt-in
        ``[llm.<name>].prompt_cache_control``, default off, and hence the plain-string default
        path being byte-identical to :meth:`generate_text_with_usage`.
        """
        return self._chat(
            self._content_for(parts),
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _content_for(self, parts: PromptParts) -> object:
        """The user message's ``content``: one plain string, or two marked parts.

        The plain string is the default and is byte-identical to what an un-split call sends.
        The two-part form is produced only when ``prompt_cache_control`` is on AND the split
        actually produced two non-empty halves — a marker covering the whole prompt would cache
        one note's values and could never hit.
        """
        if not self._prompt_cache_control or not parts.prefix or not parts.suffix:
            return parts.joined()
        return [
            {
                "type": "text",
                "text": parts.prefix,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": parts.suffix},
        ]

    def generate_json(
        self,
        parts: PromptParts,
        *,
        schema: dict[str, Any],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Ask for a schema-shaped answer, when the endpoint was configured to accept that.

        Gated on ``[llm.<name>].json_output``, default off, for the same reason
        ``prompt_cache_control`` is: ``response_format`` support is per MODEL on an aggregator
        like OpenRouter, and a model that rejects it fails the call outright. With the flag off
        this is an ordinary (still prefix-cacheable) text call and the caller's defensive parse
        does the work it would have had to do anyway.
        """
        if not self._json_output:
            return self.generate_cached_text(
                parts, system=system, temperature=temperature, max_tokens=max_tokens
            )
        return self._chat(
            self._content_for(parts),
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "omnia_items", "schema": schema},
            },
        )

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
        data, _usage = self.generate_image_with_usage(prompt, size=size)
        return data

    def generate_image_with_usage(
        self, prompt: str, *, size: str = "1024x1024"
    ) -> tuple[bytes, Optional[dict[str, int]]]:
        payload = {
            "model": self._image_model,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
        }
        resp = self._http.post_json(
            f"{self._base_url}/images/generations", payload, headers=self._headers()
        )
        # The images endpoint reports no token usage; return None (and clear last_usage) so the
        # image call is never attributed a stale text-call usage from shared state.
        self.last_usage = None
        try:
            return base64.b64decode(resp["data"][0]["b64_json"]), None
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"Unexpected image response shape: {resp}") from exc
