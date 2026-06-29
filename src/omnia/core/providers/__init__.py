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
from omnia.core.providers.usage import (
    RecordingLLMProvider,
    RecordingTTSProvider,
    UsageRecorder,
    default_recorder,
)

if TYPE_CHECKING:
    from omnia.core.config.models import LLMSettings, TTSSettings
    from omnia.core.network.http import HttpClient


def split_provider_voice(value: str) -> tuple[str, str]:
    """Split a ``"provider:voice"`` Auto-detect mapping into its parts.

    Splits on the FIRST ``":"`` so a voice id that itself contains a colon stays intact.

    Args:
        value: A ``"<provider>:<voice>"`` string from ``[tts.auto_voices]``.

    Returns:
        ``(provider, voice)``; both empty for a blank value, ``voice`` empty when no colon.
    """
    provider, sep, voice = value.partition(":")
    if not sep:
        return value, ""
    return provider, voice


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
        recorder: Optional[UsageRecorder] = None,
    ) -> None:
        self._llm_settings = llm_settings
        self._tts_settings = tts_settings
        self._http = http
        # Every built provider is wrapped so each generation records usage (calls + rough
        # char counts) for the Account dialog. Defaults to the process-wide recorder set at
        # bootstrap (a no-op until then).
        self._recorder = recorder if recorder is not None else default_recorder()
        # Providers built for a per-rule (provider, model, image_model) override, cached so
        # repeated rules reuse one instance instead of rebuilding it for every note (the wrapped
        # instance is cached, so the recording wrapper is reused too).
        self._llm_cache: dict[tuple[str, str, str], LLMProvider] = {}

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
            data = active.dict()
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

    def _tts_config(self, provider: str = "") -> dict[str, Any]:
        """Flatten the active (or named ``provider``) ``[tts.<provider>]`` subsection.

        ``provider`` selects a non-active subsection (e.g. an Auto-detect voice's provider, or a
        per-field override); empty = the configured active provider. Keeps the google_cloud
        vertex-auth bridge for whichever provider is google_cloud.
        """
        settings = self._tts_settings
        if settings is None:
            return {"provider": provider} if provider else {}
        name = provider or settings.provider
        config: dict[str, Any] = {"provider": name}
        sub = getattr(settings, name, None)
        if isinstance(sub, BaseModel):
            config.update(sub.dict())
        # google_cloud authenticates with the same Google service account as gemini_vertex.
        if name == "google_cloud":
            config = {**self._vertex_auth(), **config}
        return config

    def llm(
        self, *, model: str = "", image_model: str = "", provider: str = ""
    ) -> LLMProvider:
        """Build an LLM provider, optionally pinned to a different ``provider``/model.

        With everything empty, returns the configured active provider. A smart-notes rule may
        pin its own ``provider`` and model; the model is fixed at construction (never threaded
        per call), so the hub builds a provider whose config has the model (and ``provider``)
        overridden — caching by ``(provider, model, image_model)`` so repeated rules reuse one
        instance. ``model`` overrides the text/chat model; ``image_model`` overrides the image
        model — they are distinct fields on the same provider, so an image rule pins
        ``image_model`` (a text rule pins ``model``) and never clobbers the other.

        Args:
            provider: Override the active provider name (empty = the configured one).
            model: Override the text model id (empty = the subsection's configured model).
            image_model: Override the image model id (empty = the subsection's configured one).
        """
        if not model and not image_model and not provider:
            config = self._llm_config()
            built = create_llm_provider(config, self._http)
            return self._record_llm(built, config)
        key = (provider, model, image_model)
        cached = self._llm_cache.get(key)
        if cached is None:
            config = self._llm_config(provider)
            if model:
                config["model"] = model
            if image_model:
                config["image_model"] = image_model
            built = create_llm_provider(config, self._http)
            cached = self._record_llm(built, config)
            self._llm_cache[key] = cached
        return cached

    def tts(self, *, provider: str = "") -> TTSProvider:
        """Build a TTS provider, optionally pinned to a different ``provider``.

        Empty ``provider`` builds the configured active provider; a named one builds that
        provider (e.g. a sound field's pinned provider, or an Auto-detect voice's provider).
        Wrapped so each synthesis records usage.

        Args:
            provider: Override the active provider name (empty = the configured one).
        """
        built = create_tts_provider(self._tts_config(provider), self._http)
        return RecordingTTSProvider(built, self._recorder)

    def resolve_auto_voice(self, lang: str) -> tuple[str, str]:
        """Resolve the global Auto-detect ``(provider, voice)`` for a language code.

        Looks ``lang`` up in ``[tts.auto_voices]`` and splits the stored ``"provider:voice"``
        string. This is the SOLE source of truth for an Auto-detect field's voice — it never
        consults the catalog or any fetched/cached voice list, so a saved mapping works even on
        a machine that never refreshed voices.

        Args:
            lang: The detected ISO 639-1 language code.

        Returns:
            ``(provider, voice)`` for ``lang``.

        Raises:
            ProviderError: When ``lang`` has no Auto-detect voice configured.
        """
        mapping = self._tts_settings.auto_voices if self._tts_settings else {}
        value = mapping.get(lang, "")
        # A present mapping resolves; the voice MAY be empty for a language-only provider (e.g.
        # "google_translate:"), which synthesizes from the language directly.
        if not value:
            raise ProviderError(
                f"No Auto-detect voice set for language {lang!r} — configure it in "
                "Sound → Auto-detect voices."
            )
        provider, voice = split_provider_voice(value)
        if not provider:
            raise ProviderError(
                f"No Auto-detect voice set for language {lang!r} — configure it in "
                "Sound → Auto-detect voices."
            )
        return provider, voice

    def _record_llm(self, provider: LLMProvider, config: dict[str, Any]) -> LLMProvider:
        """Wrap ``provider`` so each generation records usage under the right model.

        Text records under ``config['model']`` (the resolved text model) and image under
        ``config['image_model']`` — the two are distinct on the same provider, so the recorder
        must not log an image call under the text model.
        """
        return RecordingLLMProvider(
            provider,
            self._recorder,
            model=str(config.get("model", "")) or "(default)",
            image_model=str(config.get("image_model", "")),
        )


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
    "split_provider_voice",
]
