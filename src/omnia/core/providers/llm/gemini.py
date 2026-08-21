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
from omnia.core.providers.llm.base import LLMProvider, PromptParts
from omnia.core.providers.llm.registry import register_llm

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_logger = get_logger("gemini")

# At or below this many output tokens, ask the model NOT to think first.
#
# Gemini's reasoning models (2.5-*, 3.x) spend ``maxOutputTokens`` on internal thoughts BEFORE
# any visible text, and the thoughts are not in the response. A caller asking for a handful of
# tokens is asking for a label — a language code, a yes/no — and reliably gets back
# ``finishReason="MAX_TOKENS"`` with no text part at all: the budget went entirely on thinking.
# That is not a hypothetical; it is why smart-notes' Auto-detect voice failed on every tts field
# (``detect_language`` asks for 8 tokens) while a pinned voice worked.
#
# The threshold, rather than always: above it the cap is an envelope around real prose (a K-note
# chunk sizes its cap to the answers it expects), and switching thinking off there would change
# the quality of ordinary generation for every Gemini user — a decision that is not this fix's
# to make.
_THINKING_OFF_MAX_TOKENS = 256


def _usage_from_gemini(resp: Any) -> Optional[dict[str, int]]:
    """Extract token usage from a Gemini ``generateContent`` response (None if absent).

    ``cachedContentTokenCount`` — how much of ``promptTokenCount`` the model served from its
    prompt cache — is reported under ``"cached"`` and ONLY when it is non-zero, mirroring
    Gemini, which omits the key entirely on a miss. Without it, prompt caching is a change with
    no way to tell whether it engaged.
    """
    meta = resp.get("usageMetadata") if isinstance(resp, dict) else None
    if not isinstance(meta, dict):
        return None
    usage = {
        "in": int(meta.get("promptTokenCount", 0)),
        "out": int(meta.get("candidatesTokenCount", 0)),
        "total": int(meta.get("totalTokenCount", 0)),
    }
    cached = int(meta.get("cachedContentTokenCount", 0) or 0)
    if cached:
        usage["cached"] = cached
    return usage


@register_llm("gemini")
class GeminiProvider(LLMProvider):
    """Talks to Google's Generative Language API (AI Studio, API-key auth)."""

    name = "gemini"
    # Gemini caches implicitly: a request whose ``contents`` START with the same tokens as a
    # recent one is discounted automatically, with nothing extra on the wire. That is exactly
    # the shape ``PromptParts`` produces, so the inherited concatenating ``generate_cached_text``
    # already benefits — there is nothing to override, only ``cachedContentTokenCount`` to
    # report. (EXPLICIT ``cachedContents`` is out of scope: it needs a create POST and a DELETE
    # to clean up, and HttpClient has no DELETE.)
    supports_prompt_cache = True
    # Gemini enforces a response schema natively (``generationConfig.responseMimeType`` +
    # ``responseSchema``), so this is a provider-level capability rather than a per-model
    # opt-in. A model too old to accept the schema answers 400, which the only caller
    # (K-note batching) treats as an ordinary chunk failure and retries per note — the JSON
    # mode is an optimisation nothing depends on, by construction.
    supports_json_output = True

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.6-flash",
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

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> GeminiProvider:
        """Build the AI-Studio Gemini provider from its config subsection.

        Args:
            config: The provider's config subsection.
            http: Optional HTTP client to inject.

        Returns:
            The configured provider.
        """
        return cls(
            api_key=config.get("api_key", ""),
            model=config.get("model", "gemini-3.6-flash"),
            image_model=config.get("image_model", ""),
            temperature=float(config.get("temperature", 0.7)),
            http=http,
        )

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
            if max_tokens <= _THINKING_OFF_MAX_TOKENS:
                # Best effort — see _THINKING_OFF_MAX_TOKENS for why, and _post for what happens
                # when a model refuses a zero budget (2.5 Pro cannot fully disable thinking).
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}
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

    def _post(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload``, retrying ONCE without ``thinkingConfig`` if the model refuses it.

        Turning thinking off is an optimisation, never a requirement, so it must not be able to
        break a call that would otherwise have worked. Gemini 2.5 Pro cannot disable thinking at
        all and answers 400 for a zero budget; a future model may do the same. One retry with the
        key removed costs a wasted round trip in that case and keeps the provider working, where
        letting the 400 through would fail the field over a knob the caller never asked for.

        Narrow on purpose: only a 400 whose message mentions thinking is retried, and only when
        the request actually carried the key. Anything else is the caller's error to see.
        """
        headers = self._headers()
        try:
            return self._http.post_json(self._endpoint(model), payload, headers=headers)
        except ProviderError as exc:
            gen_config = payload.get("generationConfig")
            if not isinstance(gen_config, dict) or "thinkingConfig" not in gen_config:
                raise
            if exc.status_code != 400 or "thinking" not in str(exc).lower():
                raise
            _logger.debug("gemini: model refused thinkingBudget=0, retrying without it")
            gen_config.pop("thinkingConfig", None)
            return self._http.post_json(self._endpoint(model), payload, headers=headers)

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
        resp = self._post(self._model, payload)
        # Return the usage parsed from THIS response; also set last_usage for external readers.
        usage = _usage_from_gemini(resp)
        self.last_usage = usage
        return self._parse_response(resp), usage

    def generate_json(
        self,
        parts: PromptParts,
        *,
        schema: dict[str, Any],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Ask Gemini to answer in JSON matching ``schema``.

        The whole payload is the same one :meth:`generate_text_with_usage` builds — the prompt
        still arrives as one leading ``contents`` part, so implicit prefix caching keeps working
        — with ``responseMimeType`` and ``responseSchema`` added to ``generationConfig``. One
        override covers both registered names: Vertex inherits this wire format unchanged and
        differs only in host and auth.
        """
        temp = self._temperature if temperature is None else temperature
        payload = self._build_payload(parts.joined(), system, temp, max_tokens)
        gen_config = payload["generationConfig"]
        gen_config["responseMimeType"] = "application/json"
        gen_config["responseSchema"] = schema
        resp = self._post(self._model, payload)
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
