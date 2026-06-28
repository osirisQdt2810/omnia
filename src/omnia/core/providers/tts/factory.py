"""Build a :class:`TTSProvider` from a config dict (with an optional injected HttpClient)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.http import HttpClient
from omnia.core.providers.token_source import resolve_token_source
from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.edge_tts import EdgeTTS
from omnia.core.providers.tts.google_cloud import GoogleCloudTTS
from omnia.core.providers.tts.google_translate import GoogleTranslateTTS
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.piper import PiperTTS

_OPENAI_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "https://api.openai.com/v1",
}


def _build_google_translate(
    config: dict[str, Any], http: Optional[HttpClient]
) -> TTSProvider:
    return GoogleTranslateTTS(
        lang=config.get("lang", "en"), tld=config.get("tld", "com"), http=http
    )


def _build_openai(config: dict[str, Any], http: Optional[HttpClient]) -> TTSProvider:
    provider = config.get("provider", "openai_compatible")
    base_url = config.get("base_url") or _OPENAI_DEFAULTS.get(
        provider, _OPENAI_DEFAULTS["openai"]
    )
    return OpenAICompatibleTTS(
        api_key=config.get("api_key", ""),
        base_url=base_url,
        model=config.get("model") or "gpt-4o-mini-tts",
        voice=config.get("voice") or "alloy",
        http=http,
    )


def _build_google_cloud(
    config: dict[str, Any], http: Optional[HttpClient]
) -> TTSProvider:
    # Reuses the same Google auth strategies as gemini_vertex (the ProviderHub merges the
    # Vertex auth fields into this config when the active TTS provider is google_cloud).
    token_source = resolve_token_source(config, http or _DEFAULT_HTTP())
    return GoogleCloudTTS(
        token_source=token_source,
        lang=config.get("lang", "en"),
        voice=config.get("voice", ""),
        language_code=config.get("language_code", ""),
        speaking_rate=float(config.get("speaking_rate", 1.0)),
        http=http,
    )


def _build_edge(config: dict[str, Any], _http: Optional[HttpClient]) -> TTSProvider:
    return EdgeTTS(lang=config.get("lang", "en"), voice=config.get("voice", ""))


def _build_piper(config: dict[str, Any], _http: Optional[HttpClient]) -> TTSProvider:
    return PiperTTS(model=config.get("model", ""))


def _DEFAULT_HTTP() -> HttpClient:  # noqa: N802 - tiny lazy accessor
    from omnia.core.providers.http import DEFAULT_HTTP_CLIENT

    return DEFAULT_HTTP_CLIENT


_BUILDERS: dict[str, Callable[[dict[str, Any], Optional[HttpClient]], TTSProvider]] = {
    "google_translate": _build_google_translate,
    "openai": _build_openai,
    "openrouter": _build_openai,
    "openai_compatible": _build_openai,
    "google_cloud": _build_google_cloud,
    "edge_tts": _build_edge,
    "piper": _build_piper,
}

# name -> provider class, so callers can read each provider's `requires_api` WITHOUT building
# it (no creds needed). Kept in sync with _BUILDERS by test_provider_metadata.
_PROVIDER_CLASSES: dict[str, type[TTSProvider]] = {
    "google_translate": GoogleTranslateTTS,
    "openai": OpenAICompatibleTTS,
    "openrouter": OpenAICompatibleTTS,
    "openai_compatible": OpenAICompatibleTTS,
    "google_cloud": GoogleCloudTTS,
    "edge_tts": EdgeTTS,
    "piper": PiperTTS,
}


def create_tts_provider(
    config: dict[str, Any], http: Optional[HttpClient] = None
) -> TTSProvider:
    """Instantiate the TTS provider named by ``config['provider']`` (default free gTTS).

    Raises:
        ProviderError: If the provider name is unknown.
    """
    provider = config.get("provider", "google_translate")
    builder = _BUILDERS.get(provider)
    if builder is None:
        raise ProviderError(
            f"Unknown TTS provider {provider!r}; known: {sorted(_BUILDERS)}"
        )
    return builder(config, http)


def available_tts_providers() -> list[str]:
    """Return the registered TTS provider names (for the settings GUI)."""
    return sorted(_BUILDERS)


def available_tts_providers_requiring_api() -> list[str]:
    """TTS providers that need an API key / cloud credentials (skippable in real tests)."""
    return sorted(n for n, c in _PROVIDER_CLASSES.items() if c.requires_api)


def available_keyless_tts_providers() -> list[str]:
    """TTS providers callable WITHOUT a key (google_translate, edge_tts, piper — must run)."""
    return sorted(n for n, c in _PROVIDER_CLASSES.items() if not c.requires_api)
