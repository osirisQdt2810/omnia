"""Thin, functions-only aggregator over the provider-layer catalog data.

The provider/model/voice/language LITERALS live WITH the providers — the LLM data in
:mod:`omnia.core.providers.llm` (``LLM_PROVIDERS``/``_TEXT_MODELS``/``_IMAGE_MODELS``), and the
TTS data in :mod:`omnia.core.providers.tts` (``TTS_PROVIDERS``/``LANGUAGES`` + each provider's
own ``CURATED_VOICES`` aggregated via :func:`~omnia.core.providers.tts.voices_for`). This module
holds ONLY the functions that shape that data into the payload the Smart Notes page bakes in;
it declares no provider literals of its own (just the generation-kind markers). Two rules keep
the dropdowns from going stale:

* a user's own saved model/voice string is ALWAYS preserved by the GUI even if it is not in the
  list (the dialog merges it in), so a missing entry never loses a configured value;
* the LLM provider subset offered for generation is intentionally smaller than every registered
  provider — ``openrouter`` already fronts the OpenAI-compatible family, so the raw ``openai`` /
  ``openai_compatible`` names are omitted from the *generation* picker.

Imports nothing from ``aqt``/``anki`` (tests headless). It imports the provider PACKAGES (data
+ the voice aggregation) but never a concrete provider module, and the provider packages never
import this one (the dependency runs one way).
"""

from __future__ import annotations

from omnia.core.providers.llm import (
    _IMAGE_MODELS,
    _TEXT_MODELS,
    LLM_PROVIDERS,
)
from omnia.core.providers.tts import (
    _LANGUAGE_ONLY_TTS_PROVIDERS,
    LANGUAGES,
    TTS_PROVIDERS,
    TTSVoice,
    aggregated_voices,
    voices_for,
)

# Re-exported (for back-compat with existing importers/tests) so callers can keep doing
# ``from ...catalog import LLM_PROVIDERS / TTS_PROVIDERS / TTSVoice / LANGUAGES / voices_for``
# even though the data now lives in the provider packages.
__all__ = [
    "KIND_IMAGE",
    "KIND_TEXT",
    "KIND_TTS",
    "LANGUAGES",
    "LLM_PROVIDERS",
    "TTS_PROVIDERS",
    "TTSVoice",
    "catalog_payload",
    "image_models",
    "models_for",
    "providers_for",
    "text_models",
    "voice_options_for_language",
    "voices_for",
]

# Generation kinds a Smart Notes field can have (mirrors SmartNotesFieldConfig.type).
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_TTS = "tts"

# ISO 639-1 code → display name, for labelling the synthetic language-only TTS options.
_LANGUAGE_LABELS: dict[str, str] = {lang["code"]: lang["label"] for lang in LANGUAGES}


def providers_for(kind: str) -> list[str]:
    """Return the provider names offered for a generation ``kind``.

    text → every LLM provider; image → only the LLM providers that ACTUALLY generate images
    (the keys of ``_IMAGE_MODELS`` — e.g. openrouter has no image endpoint, so it's excluded
    and never offered for an image field); tts → the TTS providers.
    """
    if kind == KIND_TTS:
        return list(TTS_PROVIDERS)
    if kind == KIND_IMAGE:
        return list(_IMAGE_MODELS)
    return list(LLM_PROVIDERS)


def text_models(provider: str) -> list[str]:
    """Return the curated text-model ids for ``provider`` (empty if unknown)."""
    return list(_TEXT_MODELS.get(provider, []))


def image_models(provider: str) -> list[str]:
    """Return the curated image-model ids for ``provider`` (empty if unknown)."""
    return list(_IMAGE_MODELS.get(provider, []))


def models_for(provider: str, kind: str) -> list[str]:
    """Return the model ids for ``provider`` under ``kind`` (image list for image, else text)."""
    return image_models(provider) if kind == KIND_IMAGE else text_models(provider)


def _merged_voices(
    fetched: dict[str, list[TTSVoice]] | None = None,
) -> dict[str, list[TTSVoice]]:
    """The aggregated curated voices with ``fetched`` (the Refresh result) merged over them.

    A provider present in ``fetched`` (e.g. edge_tts) has its curated list REPLACED so the
    dropdowns show the full enumerated set; every other provider keeps its seed, and a
    fetched-only provider (no seed entry) is added. This is the single merge rule both the
    per-language options and the ``voices`` payload share.
    """
    merged = dict(aggregated_voices())
    if fetched:
        merged.update(fetched)
    return merged


def _options_for_language(
    lang: str, all_voices: list[TTSVoice]
) -> list[dict[str, str]]:
    """Build a language's voice options from an already-flattened, already-merged voice list.

    The free language-only providers come first (synthetic empty-voice options), then every
    named voice whose ``lang_code`` matches.
    """
    label = _LANGUAGE_LABELS.get(lang, lang)
    options: list[dict[str, str]] = [
        {"value": f"{provider}:", "label": f"{provider} · {label} (auto voice)"}
        for provider in _LANGUAGE_ONLY_TTS_PROVIDERS
    ]
    options.extend(
        {
            "value": f"{v.provider}:{v.voice}",
            "label": f"{v.provider} · {v.name} · {v.gender}",
        }
        for v in all_voices
        if v.lang_code == lang
    )
    return options


def voice_options_for_language(
    lang: str, fetched: dict[str, list[TTSVoice]] | None = None
) -> list[dict[str, str]]:
    """Return the cross-provider voice options for ``lang`` as ``{value, label}`` dicts.

    Gathers every voice (across ALL providers) whose ``lang_code`` matches ``lang``, as
    ``{"value": "<provider>:<voice>", "label": "<provider> · <name> · <gender>"}``. With
    ``fetched`` given (a provider→voices map from Refresh), that provider's fetched voices
    replace its curated seed so the dropdown reflects the full enumerated set.

    Every language also gets one synthetic option per language-only provider (e.g.
    ``google_translate``): it has no named voices but serves any language for free, so the
    option's voice is empty and synthesis uses the language directly.

    Args:
        lang: The ISO 639-1 language code to filter by.
        fetched: Optional fetched voices per provider (merged over the seed).

    Returns:
        The voice options for that language: the free language-only providers first, then every
        named voice whose ``lang_code`` matches.
    """
    all_voices = [v for voices in _merged_voices(fetched).values() for v in voices]
    return _options_for_language(lang, all_voices)


def catalog_payload(
    fetched_voices: dict[str, list[TTSVoice]] | None = None,
) -> dict[str, object]:
    """Build the JSON-able catalog the Smart Notes page bakes in to drive its dropdowns.

    Shape::

        {
          "llm_providers": [...],            # for text/image kinds
          "tts_providers": [...],            # for the tts kind
          "text_models":  {provider: [id, ...]},
          "image_models": {provider: [id, ...]},
          "voices":       {provider: [{"voice", "label", "language", "gender", "model"}, ...]},
          "auto_voice_options": {lang: [{"value", "label"}, ...]},  # the Auto-detect editor
        }

    The page reads ``llm_providers``/``tts_providers`` to fill the Provider dropdown by kind,
    ``text_models``/``image_models`` to fill the Model dropdown, ``voices`` to fill the per-row
    Voice dropdown, and ``auto_voice_options`` to fill each language's Auto-detect dropdown.

    Args:
        fetched_voices: Optional Refresh result (provider→voices) merged over the seed when
            building ``auto_voice_options``/``voices`` (offline-safe: ``None`` uses the seed).
    """
    # Aggregate + merge the fetched voices ONCE, then reuse for both the per-language options
    # and the per-provider ``voices`` payload (no re-aggregation per language).
    voices_by_provider = _merged_voices(fetched_voices)
    all_voices = [v for voices in voices_by_provider.values() for v in voices]
    return {
        "llm_providers": list(LLM_PROVIDERS),
        "image_providers": providers_for(KIND_IMAGE),
        "tts_providers": list(TTS_PROVIDERS),
        "languages": [dict(lang) for lang in LANGUAGES],
        "auto_voice_options": {
            lang["code"]: _options_for_language(lang["code"], all_voices)
            for lang in LANGUAGES
            if lang["code"]
        },
        "text_models": {p: text_models(p) for p in LLM_PROVIDERS},
        "image_models": {p: image_models(p) for p in LLM_PROVIDERS},
        "voices": {
            provider: [
                {
                    "voice": v.voice,
                    "label": v.label,
                    "language": v.language,
                    "gender": v.gender,
                    "model": v.model,
                }
                for v in voices
            ]
            for provider, voices in voices_by_provider.items()
        },
    }
