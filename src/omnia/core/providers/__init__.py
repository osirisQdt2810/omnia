"""Provider layer: LLM + TTS abstractions and a small hub that builds them from config.

Features depend on the :class:`~omnia.core.providers.llm.LLMProvider` /
:class:`~omnia.core.providers.tts.TTSProvider` interfaces, never on a concrete SDK
(ADR-004). The :class:`ProviderHub` is handed to plugins via the ``PluginContext``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

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

    def _llm_config(self) -> dict[str, Any]:
        settings = self._llm_settings
        if settings is None:
            return {}
        config: dict[str, Any] = {"provider": settings.provider}
        active = settings.active()
        if active is not None:
            data = active.model_dump()
            # The factory/providers use ``model`` for the chat model; settings use text_model.
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

    def llm(self) -> LLMProvider:
        """Build the configured LLM provider."""
        return create_llm_provider(self._llm_config(), self._http)

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
