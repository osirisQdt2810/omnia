"""Provider layer: LLM + TTS abstractions and a small hub that builds them from config.

Features depend on the :class:`~omnia.core.providers.llm.LLMProvider` /
:class:`~omnia.core.providers.tts.TTSProvider` interfaces, never on a concrete SDK
(ADR-004). The :class:`ProviderHub` is handed to plugins via the ``PluginContext``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Optional, cast

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
    the active ``[llm.<provider>]`` subsection and flattens it into the dict the registry
    expects (``text_model`` → ``model``). ``google_cloud`` TTS reuses the Google auth that
    lives under ``[llm.gemini_vertex]``, so the hub bridges those fields in.
    """

    def __init__(
        self,
        llm_settings: Optional[LLMSettings] = None,
        tts_settings: Optional[TTSSettings] = None,
        http: Optional[HttpClient] = None,
        recorder: Optional[UsageRecorder] = None,
        *,
        config: Any = None,
    ) -> None:
        # ``config`` is a ConfigRepository-like object exposing ``llm_settings()`` /
        # ``tts_settings()``. When given, the hub reads settings FRESH on every access, so a live
        # config change (e.g. an Auto-detect voice edit) is picked up without an Anki restart. The
        # positional ``llm_settings``/``tts_settings`` snapshots stay for the dialog one-shot hubs
        # and tests and are used only when ``config`` is None. Typed ``Any`` to avoid a hard import
        # cycle with omnia.core.config.
        self._config = config
        self._llm_settings_static = llm_settings
        self._tts_settings_static = tts_settings
        self._http = http
        # Every built provider is wrapped so each generation records usage (calls + rough
        # char counts) for the Account dialog. Defaults to the process-wide recorder set at
        # bootstrap (a no-op until then).
        self._recorder = recorder if recorder is not None else default_recorder()
        # Providers built for a per-rule (provider, model, image_model) override, cached so
        # repeated rules reuse one instance instead of rebuilding it for every note (the wrapped
        # instance is cached, so the recording wrapper is reused too).
        self._llm_cache: dict[tuple[str, str, str], LLMProvider] = {}
        # The LLMSettings object the override cache was populated against. A config reload yields a
        # NEW settings object, so a mismatch means the cached providers are stale (built for old
        # config) and must be dropped. Stays None for snapshot hubs (config is None).
        self._llm_cache_ref: Optional[LLMSettings] = None
        # The same treatment for TTS, and for the same measured reason: ``tts()`` is called once
        # per audio FIELD (ResolvedVoice.for_rule), and for google_cloud each rebuild mints a
        # fresh ServiceAccountTokenSource with an empty token cache — an RS256 sign in pure
        # Python plus an OAuth round trip per synthesis. Keyed on the provider name alone: unlike
        # llm(), a TTS provider carries no per-rule model/voice on the instance (the voice is a
        # per-call argument), so one instance serves every rule that names that provider.
        self._tts_cache: dict[str, TTSProvider] = {}
        self._tts_cache_ref: Optional[TTSSettings] = None
        # ``llm()``/``tts()`` run on background generation threads (QueryOp); guard every
        # read/mutate of the provider caches + their refs so concurrent callers can't corrupt a
        # dict or double-build. ONE lock for both: construction is short, the two are never held
        # nested, and two locks would be two things to reason about for no measurable gain.
        self._cache_lock = threading.Lock()

    @property
    def _llm_settings(self) -> Optional[LLMSettings]:
        """The current LLM settings — read FRESH from the repo when one was injected.

        With a ``config`` repo, returns its latest ``llm_settings()`` (a new object after each
        reload) so a live edit is seen without a restart; otherwise the constructor snapshot. All
        internal reads go through this property, so they transparently pick up the fresh object.
        """
        if self._config is not None:
            # ``_config`` is Any (loosely typed to avoid an import cycle); the repo contract is
            # that ``llm_settings()`` returns LLMSettings.
            return cast("LLMSettings", self._config.llm_settings())
        return self._llm_settings_static

    @property
    def _tts_settings(self) -> Optional[TTSSettings]:
        """The current TTS settings — read FRESH from the repo when one was injected.

        See :attr:`_llm_settings`; same fresh-vs-snapshot rule for the TTS side.
        """
        if self._config is not None:
            # See :attr:`_llm_settings`: ``_config`` is Any; the repo returns TTSSettings here.
            return cast("TTSSettings", self._config.tts_settings())
        return self._tts_settings_static

    def _maybe_invalidate_cache(self) -> None:
        """Drop the per-rule override cache when the LLM settings object changed.

        A config reload rebuilds ``LLMSettings`` into a new object; providers cached against the
        old one are stale. No-op for snapshot hubs (``config`` is None), whose settings never
        change under them.
        """
        if self._config is None:
            return
        cur = self._llm_settings  # fresh
        if cur is not self._llm_cache_ref:
            self._llm_cache.clear()
            self._llm_cache_ref = cur

    def _maybe_invalidate_tts_cache(self) -> None:
        """The TTS twin of :meth:`_maybe_invalidate_cache`.

        Separate from the LLM one because the two settings objects reload independently: a voice
        edit rebuilds ``TTSSettings`` and must not throw away LLM providers whose token sources
        are still warm, and vice versa.
        """
        if self._config is None:
            return
        cur = self._tts_settings  # fresh
        if cur is not self._tts_cache_ref:
            self._tts_cache.clear()
            self._tts_cache_ref = cur

    def _llm_config(self, provider: str = "") -> dict[str, Any]:
        """Flatten the active (or named ``provider``) ``[llm.<provider>]`` subsection.

        Maps ``text_model`` → ``model`` for the registry; ``image_model`` passes through.
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
            # The registry/providers use ``model`` for the chat model; settings use text_model.
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
        # Hold the cache lock across invalidate + get + build + set so concurrent generation
        # threads can't race the dict. Construction is CHEAP but not always network-free: a
        # gemini_vertex provider resolves a service-account token source, whose first use signs
        # an RS256 JWT and POSTs for an access token. That is exactly why the unpinned path is
        # cached too — see the key below.
        with self._cache_lock:
            self._maybe_invalidate_cache()
            # The UNPINNED call (a rule that pins neither provider nor model — the default for
            # every field) resolves to the active provider, so it keys the same cache entry an
            # explicitly-pinned call to that provider would. Leaving it uncached rebuilt the
            # provider — and with it a token source holding an EMPTY token cache — for every
            # single field of every single note, turning one JWT sign + OAuth round trip per
            # generation into the dominant cost of a batch.
            key = (provider or self._active_llm_name(), model, image_model)
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

    def _active_llm_name(self) -> str:
        """The configured active LLM provider name (empty when there are no settings)."""
        settings = self._llm_settings
        return str(settings.provider) if settings is not None else ""

    def tts(self, *, provider: str = "") -> TTSProvider:
        """Build a TTS provider, optionally pinned to a different ``provider``.

        Empty ``provider`` builds the configured active provider; a named one builds that
        provider (e.g. a sound field's pinned provider, or an Auto-detect voice's provider).
        Wrapped so each synthesis records usage.

        Cached, under the same lock and for the same measured reason as :meth:`llm`: this is
        called once per audio field of every note, and a google_cloud rebuild mints a fresh
        service-account token source whose empty cache turns one RS256 sign (pure-Python, so it
        holds the GIL and serialises the workers) plus one OAuth round trip into a per-synthesis
        cost. The unpinned call keys the same entry a call naming the active provider would, so
        the default path is cached too — the omission that made the LLM version slow.

        Args:
            provider: Override the active provider name (empty = the configured one).
        """
        with self._cache_lock:
            self._maybe_invalidate_tts_cache()
            key = provider or self._active_tts_name()
            cached = self._tts_cache.get(key)
            if cached is None:
                built = create_tts_provider(self._tts_config(provider), self._http)
                cached = RecordingTTSProvider(built, self._recorder)
                self._tts_cache[key] = cached
            return cached

    def _active_tts_name(self) -> str:
        """The configured active TTS provider name (empty when there are no settings)."""
        settings = self._tts_settings
        return str(settings.provider) if settings is not None else ""

    def resolve_auto_voice(self, lang: str, *, reason: str = "") -> tuple[str, str]:
        """Resolve the global Auto-detect ``(provider, voice)`` for a language code.

        Looks ``lang`` up in ``[tts.auto_voices]`` and splits the stored ``"provider:voice"``
        string. This is the SOLE source of truth for an Auto-detect field's voice — it never
        consults the catalog or any fetched/cached voice list, so a saved mapping works even on
        a machine that never refreshed voices.

        Args:
            lang: The detected ISO 639-1 language code.
            reason: Why detection produced no code, when that is what happened. Quoted verbatim
                in the error — a caller that swallowed the provider's own message is the only
                thing that knows it, and without it the user is told to configure something
                they already configured.

        Returns:
            ``(provider, voice)`` for ``lang``.

        Raises:
            ProviderError: When ``lang`` has no Auto-detect voice configured.
        """
        mapping = self._tts_settings.auto_voices if self._tts_settings else {}
        lang = (lang or "").strip()
        if not lang:
            # The language could not be detected at all — no LLM configured, a provider error,
            # or nothing to detect from. Blaming the Auto-detect map for language '' (which is
            # what this used to do) points the user at the wrong setting entirely.
            if len(mapping) == 1:
                # Exactly one Auto-detect voice is configured, so there is nothing to choose
                # between: use it rather than fail over a detection that was only advisory.
                value = next(iter(mapping.values()))
            else:
                detail = f" Detection failed with: {reason}." if reason else ""
                raise ProviderError(
                    "Could not detect the language of the text, so no Auto-detect voice could "
                    f"be chosen.{detail} Pin a voice on the field, set the field's Language, or "
                    "make sure a text provider is configured for language detection."
                )
        else:
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
