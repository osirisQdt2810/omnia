"""TTS provider package — interface, factory, and the aggregated voice catalog.

This package is the SINGLE source of TTS voice data: each provider declares its own curated
voices on its class (:attr:`TTSProvider.CURATED_VOICES`) and exposes them polymorphically via
:meth:`TTSProvider.list_voices`. :func:`voices_for` / :func:`refresh_voices` aggregate across
providers so the settings catalog/GUI never import a concrete provider. Pure module — no Anki
imports; never imports ``omnia.core.providers.catalog`` (the dependency runs the other way).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

# Import every provider module so its ``@register_tts`` runs at package import (mirrors how
# ``plugins/__init__.py`` imports each feature). The registry is empty until these run.
from omnia.core.providers.tts import (
    edge_tts,
    google_cloud,
    google_translate,
    openai_compatible,
    piper,
    viettts,
)
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import (
    TTS_REGISTRY,
    available_keyless_tts_providers,
    available_tts_providers,
    available_tts_providers_requiring_api,
    create_tts_provider,
)

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# TTS providers offered for sound generation (free/offline first; cloud after).
TTS_PROVIDERS: list[str] = [
    "edge_tts",
    "google_cloud",
    "google_translate",
    "viettts",
    "piper",
]

# TTS providers with NO named voices that nonetheless serve ANY language (keyless, free): they
# contribute a synthetic per-language Auto-detect option (an empty voice — the provider uses
# the language directly). piper is excluded (its voice is a local .onnx path).
_LANGUAGE_ONLY_TTS_PROVIDERS: tuple[str, ...] = ("google_translate",)

# The languages the global "Auto-detect voices" editor lists, each ``{label, code}`` where
# ``code`` is the ISO 639-1 code. A broad set so a detected language usually has a row to map;
# there is no per-field Language picker (a voice fixes the language, else the engine
# auto-detects and resolves through [tts.auto_voices]).
LANGUAGES: list[dict[str, str]] = [
    {"code": "ar", "label": "Arabic"},
    {"code": "bn", "label": "Bengali"},
    {"code": "bg", "label": "Bulgarian"},
    {"code": "ca", "label": "Catalan"},
    {"code": "zh", "label": "Chinese"},
    {"code": "hr", "label": "Croatian"},
    {"code": "cs", "label": "Czech"},
    {"code": "da", "label": "Danish"},
    {"code": "nl", "label": "Dutch"},
    {"code": "en", "label": "English"},
    {"code": "fi", "label": "Finnish"},
    {"code": "fr", "label": "French"},
    {"code": "de", "label": "German"},
    {"code": "el", "label": "Greek"},
    {"code": "he", "label": "Hebrew"},
    {"code": "hi", "label": "Hindi"},
    {"code": "hu", "label": "Hungarian"},
    {"code": "id", "label": "Indonesian"},
    {"code": "it", "label": "Italian"},
    {"code": "ja", "label": "Japanese"},
    {"code": "ko", "label": "Korean"},
    {"code": "ms", "label": "Malay"},
    {"code": "no", "label": "Norwegian"},
    {"code": "fa", "label": "Persian"},
    {"code": "pl", "label": "Polish"},
    {"code": "pt", "label": "Portuguese"},
    {"code": "ro", "label": "Romanian"},
    {"code": "ru", "label": "Russian"},
    {"code": "sk", "label": "Slovak"},
    {"code": "es", "label": "Spanish"},
    {"code": "sv", "label": "Swedish"},
    {"code": "ta", "label": "Tamil"},
    {"code": "te", "label": "Telugu"},
    {"code": "th", "label": "Thai"},
    {"code": "tr", "label": "Turkish"},
    {"code": "uk", "label": "Ukrainian"},
    {"code": "vi", "label": "Vietnamese"},
]


def _provider_classes() -> list[type[TTSProvider]]:
    """The distinct TTS provider classes (deduped — several registry names share one class).

    Read from the registry so voice listing stays in lockstep with what can be built, without
    instantiating anything (curated voices are class-level).
    """
    seen: set[type[TTSProvider]] = set()
    classes: list[type[TTSProvider]] = []
    for cls in TTS_REGISTRY.values():
        if cls not in seen:
            seen.add(cls)
            classes.append(cls)
    return classes


def _group_by_provider(voices: list[TTSVoice]) -> dict[str, list[TTSVoice]]:
    """Group voices by their own ``provider`` tag (keeps the catalog's historical keys).

    A voice's ``provider`` field is its catalog key (e.g. the OpenAI-compatible class tags its
    voices ``"openai"``), so the aggregated map is keyed the same way the GUI/config expect.
    """
    grouped: dict[str, list[TTSVoice]] = {}
    for voice in voices:
        grouped.setdefault(voice.provider, []).append(voice)
    return grouped


def aggregated_voices(
    http: Optional[HttpClient] = None, *, refresh: bool = False
) -> dict[str, list[TTSVoice]]:
    """Aggregate every provider's voices into ``{provider: [TTSVoice, ...]}``.

    Calls :meth:`TTSProvider.list_voices` on each provider class. With ``refresh`` the
    fetch-capable providers (e.g. edge_tts) fetch + merge their live list; the rest return their
    curated seed. Offline-safe: ``refresh=False`` never touches the network.

    Args:
        http: HTTP client for live fetches (only used when ``refresh`` is set).
        refresh: Fetch the live list from fetch-capable providers when True.

    Returns:
        The aggregated voice map, keyed by each voice's ``provider`` tag.
    """
    all_voices: list[TTSVoice] = []
    for cls in _provider_classes():
        all_voices.extend(cls.list_voices(http, refresh=refresh))
    return _group_by_provider(all_voices)


def voices_for(provider: str) -> list[TTSVoice]:
    """Return the curated voices for a TTS ``provider`` (empty when not enumerable offline)."""
    return list(aggregated_voices().get(provider, []))


def refresh_voices(http: Optional[HttpClient] = None) -> dict[str, list[TTSVoice]]:
    """Rebuild the aggregated voice map, fetching the live list from fetch-capable providers.

    Network I/O (the fetch-capable providers) — run it off the Qt main thread. Provider-agnostic:
    callers never name a concrete provider; each provider decides whether it can fetch.
    """
    return aggregated_voices(http, refresh=True)


__all__ = [
    "LANGUAGES",
    "TTS_PROVIDERS",
    "TTSProvider",
    "TTSVoice",
    "aggregated_voices",
    "available_keyless_tts_providers",
    "available_tts_providers",
    "available_tts_providers_requiring_api",
    "create_tts_provider",
    "refresh_voices",
    "voices_for",
]
