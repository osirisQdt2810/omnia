"""Provider layer: LLM + TTS abstractions and a small hub that builds them from config.

Features depend on the :class:`~omnia.core.providers.llm.LLMProvider` /
:class:`~omnia.core.providers.tts.TTSProvider` interfaces, never on a concrete SDK
(ADR-004). The :class:`ProviderHub` is handed to plugins via the ``PluginContext``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm import (
    LLMProvider,
    available_keyless_llm_providers,
    available_llm_providers,
    available_llm_providers_requiring_api,
    create_llm_provider,
)
from omnia.core.providers.tts import (
    TTSProvider,
    available_keyless_tts_providers,
    available_tts_providers,
    available_tts_providers_requiring_api,
    create_tts_provider,
)

if TYPE_CHECKING:
    from omnia.core.config.models import LLMSettings, TTSSettings
    from omnia.core.providers.http import HttpClient


class ProviderHub:
    """Config-aware factory passed to plugins; builds the configured LLM/TTS providers.

    Constructed from the typed provider settings + an injected HTTP client (DIP — features
    depend on this hub, not on a concrete SDK). The LLM config is per-provider: the hub picks
    the active ``[llm.<provider>]`` subsection and flattens it into the dict the factory
    expects (``text_model`` → ``model``). ``google_cloud`` TTS reuses the Google auth that
    lives under ``[llm.gemini_vertex]``, so the hub bridges those fields in.
    """

    def __init__(
        self,
        llm_settings: Optional[LLMSettings] = None,
        tts_settings: Optional[TTSSettings] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self._llm_settings = llm_settings
        self._tts_settings = tts_settings
        self._http = http
        # Providers built for a per-rule (provider, model) override, cached so repeated rules
        # reuse one instance instead of rebuilding it for every note.
        self._llm_cache: dict[tuple[str, str], LLMProvider] = {}

    def _llm_config(self, provider: str = "") -> dict[str, Any]:
        """Flatten the active (or named ``provider``) ``[llm.<provider>]`` subsection.

        Maps ``text_model`` → ``model`` for the factory; ``image_model`` passes through.
        ``provider`` selects a non-active subsection (a per-rule override); empty = the
        configured active provider.
        """
        settings = self._llm_settings
        if settings is None:
            return {"provider": provider} if provider else {}
        name = provider or settings.provider
        config: dict[str, Any] = {"provider": name}
        active = getattr(settings, name, None)
        if isinstance(active, BaseModel):
            data = active.model_dump()
            # The factory/providers use ``model`` for the chat model; settings use text_model.
            # ``image_model`` passes through unchanged so generate_image can target it.
            data["model"] = data.pop("text_model", "")
            config.update(data)
        return config

    def _vertex_auth(self) -> dict[str, Any]:
        """The Google service-account auth from ``[llm.gemini_vertex]`` (for google_cloud TTS)."""
        if self._llm_settings is None:
            return {}
        return self._llm_settings.gemini_vertex.google_auth()

    def _tts_config(self) -> dict[str, Any]:
        settings = self._tts_settings
        if settings is None:
            return {}
        config: dict[str, Any] = {"provider": settings.provider}
        active = settings.active()
        if active is not None:
            config.update(active.model_dump())
        # google_cloud authenticates with the same Google service account as gemini_vertex.
        if settings.provider == "google_cloud":
            config = {**self._vertex_auth(), **config}
        return config

    def llm(self, *, model: str = "", provider: str = "") -> LLMProvider:
        """Build an LLM provider, optionally pinned to a different ``provider``/``model``.

        With both empty, returns the configured active provider. A smart-notes rule may pin
        its own ``provider``/``model``; the model is fixed at construction (never threaded
        per call), so the hub builds a provider whose config has ``model`` (and ``provider``)
        overridden — caching by ``(provider, model)`` so repeated rules reuse one instance.

        Args:
            provider: Override the active provider name (empty = the configured one).
            model: Override the text model id (empty = the subsection's configured model).
        """
        if not model and not provider:
            return create_llm_provider(self._llm_config(), self._http)
        key = (provider, model)
        cached = self._llm_cache.get(key)
        if cached is None:
            config = self._llm_config(provider)
            if model:
                config["model"] = model
            cached = create_llm_provider(config, self._http)
            self._llm_cache[key] = cached
        return cached

    def tts(self) -> TTSProvider:
        """Build the configured TTS provider."""
        return create_tts_provider(self._tts_config(), self._http)


__all__ = [
    "LLMProvider",
    "ProviderError",
    "ProviderHub",
    "TTSProvider",
    "available_keyless_llm_providers",
    "available_keyless_tts_providers",
    "available_llm_providers",
    "available_llm_providers_requiring_api",
    "available_tts_providers",
    "available_tts_providers_requiring_api",
    "create_llm_provider",
    "create_tts_provider",
]
