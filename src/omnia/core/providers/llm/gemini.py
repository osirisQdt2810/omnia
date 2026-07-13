"""Google Gemini (AI Studio) LLM provider.

Uses the ``generateContent`` REST endpoint. The API key is sent via the
``x-goog-api-key`` header (never in the URL/query) so it can't leak into error messages.

Designed as a **template method**: :meth:`generate_text` builds the payload, POSTs it, and
parses the result, deferring only the *host* (:meth:`_endpoint`) and *auth*
(:meth:`_headers`) to subclasses. The Vertex variant
(:class:`omnia.core.providers.llm.gemini_vertex.GeminiVertexProvider`) inherits all of this
and overrides just those two hooks — same wire format, different host + bearer-token auth.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import LLMProvider

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_logger = get_logger("gemini")


def _usage_from_gemini(resp: Any) -> Optional[dict[str, int]]:
    """Extract token usage from a Gemini ``generateContent`` response (None if absent)."""
    meta = resp.get("usageMetadata") if isinstance(resp, dict) else None
    if not isinstance(meta, dict):
        return None
    return {
        "in": int(meta.get("promptTokenCount", 0)),
        "out": int(meta.get("candidatesTokenCount", 0)),
        "total": int(meta.get("totalTokenCount", 0)),
    }


class GeminiProvider(LLMProvider):
    """Talks to Google's Generative Language API (AI Studio, API-key auth)."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        image_model: str = "",
        temperature: float = 0.7,
        http: Optional[HttpClient] = None,
    ) -> None:
        if not api_key:
            raise ProviderError("Gemini provider requires an api_key")
        self._api_key = api_key
        self._model = model
        self._image_model = image_model
        self._temperature = temperature
        self._http = http or DEFAULT_HTTP_CLIENT

    # --- hooks subclasses override (the only things that differ for Vertex) ---------
    def _endpoint(self, model: str) -> str:
        """Return the ``generateContent`` URL for ``model``."""
        return f"{_BASE_URL}/models/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        """Return the auth headers for the request."""
        return {"x-goog-api-key": self._api_key}

    # --- shared wire format (inherited unchanged by the Vertex subclass) ------------
    def _build_payload(
        self,
        prompt: str,
        system: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        """Build a Gemini ``generateContent`` request body."""
        gen_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            gen_config["maxOutputTokens"] = max_tokens
        payload: dict[str, Any] = {
            # role is REQUIRED by Vertex's generateContent ("Please use a valid role: user,
            # model."); AI Studio defaults it to "user", so setting it works for both.
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _build_image_payload(self, prompt: str) -> dict[str, Any]:
        """Build a Gemini ``generateContent`` body that asks for an inline image.

        Mirrors vio-ai's image call: the prompt rides the same ``contents`` envelope as text,
        but ``generationConfig.responseModalities`` must include ``"IMAGE"`` or the model
        returns text only and emits no picture.
        """
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }

    def _parse_response(self, resp: dict[str, Any]) -> str:
        """Extract the concatenated text from a ``generateContent`` response."""
        try:
            candidate = resp["candidates"][0]
            parts = candidate.get("content", {}).get("parts")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected Gemini response shape: {resp}") from exc
        if not parts:
            # A reasoning model (e.g. gemini-2.5-*) can spend the whole token budget on
            # internal "thoughts" and return a candidate with no text part — surface that
            # clearly instead of a generic shape error.
            reason = candidate.get("finishReason", "")
            raise ProviderError(
                f"Gemini returned no text (finishReason={reason!r}); "
                "raise max_tokens if it was truncated before producing output"
            )
        return "".join(str(part.get("text", "")) for part in parts)

    def _parse_image_response(self, resp: dict[str, Any]) -> bytes:
        """Extract inline base64 image bytes from a ``generateContent`` response.

        Gemini image models return the picture as a ``inlineData`` part (base64 ``data`` +
        a ``mimeType``) alongside any text parts; the first inline part wins.
        """
        try:
            parts = resp["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected Gemini response shape: {resp}") from exc
        for part in parts or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except (ValueError, TypeError) as exc:
                    raise ProviderError(
                        "Gemini returned an undecodable inline image"
                    ) from exc
        # No inline image part. Surface WHY: the finishReason and what DID come back (part kinds +
        # any text the model returned instead), so the failure is diagnosable rather than opaque.
        candidate = resp["candidates"][0]
        reason = candidate.get("finishReason", "")
        part_kinds = sorted({k for part in (parts or []) for k in part})
        returned_text = "".join(
            str(part.get("text", "")) for part in (parts or [])
        ).strip()
        # Log METADATA only, at DEBUG — never the raw response body, which can carry the prompt /
        # returned text (PII) into the log. finishReason + part kinds + sizes are enough to triage.
        _logger.debug(
            "gemini image: no inline image (finishReason=%r, parts=%s, text_len=%d)",
            reason,
            part_kinds or "none",
            len(returned_text),
        )
        detail = f"parts={part_kinds or 'none'}"
        if returned_text:
            snippet = returned_text[:200] + ("…" if len(returned_text) > 200 else "")
            detail += f'; returned text instead: "{snippet}"'
        raise ProviderError(
            f"Gemini returned no image data (finishReason={reason!r}; {detail}); "
            "check the configured image_model supports image output"
        )

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

    def generate_text_with_usage(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        temp = self._temperature if temperature is None else temperature
        payload = self._build_payload(prompt, system, temp, max_tokens)
        resp = self._http.post_json(
            self._endpoint(self._model), payload, headers=self._headers()
        )
        # Return the usage parsed from THIS response; also set last_usage for external readers.
        usage = _usage_from_gemini(resp)
        self.last_usage = usage
        return self._parse_response(resp), usage

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        data, _usage = self.generate_image_with_usage(prompt, size=size)
        return data

    def generate_image_with_usage(
        self, prompt: str, *, size: str = "1024x1024"
    ) -> tuple[bytes, Optional[dict[str, int]]]:
        if not self._image_model:
            raise ProviderError(
                f"{self.name} image generation needs an image_model "
                "(set [llm.<provider>].image_model)"
            )
        # generateContent against the image model, asking for an inline IMAGE modality.
        payload = self._build_image_payload(prompt)
        resp = self._http.post_json(
            self._endpoint(self._image_model), payload, headers=self._headers()
        )
        # Return the image call's exact token usage (mirrors generate_text_with_usage) so the
        # recording wrapper attributes tokens from THIS call, not a stale/absent last_usage.
        usage = _usage_from_gemini(resp)
        self.last_usage = usage
        return self._parse_image_response(resp), usage
