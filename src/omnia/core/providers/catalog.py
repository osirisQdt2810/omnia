"""Curated provider / model / voice catalog for the settings GUI.

Pure data + small helpers so the Smart Notes table can show DROPDOWNS (not free-text)
for the provider, the model, and — for sound fields — the voice. Anki runs offline and
must not block the UI enumerating models/voices over the network, so the lists are
hand-curated sensible defaults. Two rules keep them from going stale:

* a user's own saved model/voice string is ALWAYS preserved by the GUI even if it is not
  in the list (the dialog merges it in), so a missing entry never loses a configured value;
* the LLM provider subset offered for generation is intentionally smaller than every
  registered provider — ``openrouter`` already fronts the OpenAI-compatible family, so the
  raw ``openai`` / ``openai_compatible`` names are omitted from the *generation* picker.

This module imports nothing from ``aqt``/``anki`` (it tests headless) and nothing from the
concrete providers — it is plain metadata the GUI bakes into the page.
"""

from __future__ import annotations

from dataclasses import dataclass

# Generation kinds a Smart Notes field can have (mirrors SmartNotesFieldConfig.type).
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_TTS = "tts"

# LLM providers offered for text/image generation in Smart Notes. A deliberate subset of the
# registered LLM providers: openrouter already proxies the OpenAI-compatible family, so the
# bare openai/openai_compatible names are left out of the picker.
LLM_PROVIDERS: list[str] = ["gemini", "gemini_vertex", "openrouter"]

# TTS providers offered for sound generation (free/offline first; cloud after).
TTS_PROVIDERS: list[str] = ["edge_tts", "google_cloud", "google_translate", "piper"]

# Languages offered in the sound-field Language picker. ``code`` is the ISO 639-1 code passed
# to the TTS provider; the empty code means "auto-detect the spoken text's language".
LANGUAGES: list[dict[str, str]] = [
    {"code": "", "label": "Auto-detect"},
    {"code": "en", "label": "English"},
    {"code": "vi", "label": "Vietnamese"},
    {"code": "ja", "label": "Japanese"},
    {"code": "ko", "label": "Korean"},
    {"code": "zh", "label": "Chinese"},
    {"code": "fr", "label": "French"},
    {"code": "es", "label": "Spanish"},
    {"code": "de", "label": "German"},
]

# Text models per LLM provider (curated defaults; the GUI merges in the user's saved model).
_GEMINI_TEXT_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-3.0-flash",
    "gemini-3.0-pro",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]
_TEXT_MODELS: dict[str, list[str]] = {
    "gemini": list(_GEMINI_TEXT_MODELS),
    "gemini_vertex": list(_GEMINI_TEXT_MODELS),
    "openrouter": [
        "openai/gpt-4o-mini",
        "openai/gpt-4o",
        "anthropic/claude-3.5-sonnet",
        "google/gemini-2.0-flash-001",
        "meta-llama/llama-3.1-70b-instruct",
        "deepseek/deepseek-chat",
    ],
}

# Image models per LLM provider (only those that actually generate images).
_GEMINI_IMAGE_MODELS = [
    "gemini-3.1-flash-image",
    "gemini-3.0-flash-image",
    "gemini-2.5-flash-image",
    "imagen-3.0-generate-002",
]
_IMAGE_MODELS: dict[str, list[str]] = {
    "gemini": list(_GEMINI_IMAGE_MODELS),
    "gemini_vertex": list(_GEMINI_IMAGE_MODELS),
    "openrouter": ["openai/gpt-image-1"],
}


@dataclass(frozen=True)
class TTSVoice:
    """One selectable TTS voice: what to pass to ``synthesize`` + how to label it.

    ``voice`` is the exact id handed to :meth:`TTSProvider.synthesize`; ``model`` is the TTS
    model where the provider needs one (e.g. OpenAI's ``gpt-4o-mini-tts``) and empty
    otherwise. ``language``/``name``/``gender`` are display metadata — together they form the
    human label so the user reads "Vietnamese · HoaiMy · Female" instead of a raw voice id.
    """

    provider: str
    voice: str
    language: str
    name: str
    gender: str
    model: str = ""

    @property
    def label(self) -> str:
        """Human label shown in the dropdown: ``<language> · <name> · <gender>``."""
        return f"{self.language} · {self.name} · {self.gender}"


# Curated voices per TTS provider. Vietnamese + English are first-class (the add-on's main
# audience); a few common languages follow. Providers whose voice can't be enumerated offline
# (piper = a local .onnx path, google_translate = language-only) have no entries — the GUI
# falls back to the central config / auto-detect for those.
_VOICES: dict[str, list[TTSVoice]] = {
    "edge_tts": [
        TTSVoice("edge_tts", "en-US-AriaNeural", "English (US)", "Aria", "Female"),
        TTSVoice("edge_tts", "en-US-GuyNeural", "English (US)", "Guy", "Male"),
        TTSVoice("edge_tts", "en-GB-SoniaNeural", "English (UK)", "Sonia", "Female"),
        TTSVoice("edge_tts", "vi-VN-HoaiMyNeural", "Vietnamese", "HoaiMy", "Female"),
        TTSVoice("edge_tts", "vi-VN-NamMinhNeural", "Vietnamese", "NamMinh", "Male"),
        TTSVoice("edge_tts", "ja-JP-NanamiNeural", "Japanese", "Nanami", "Female"),
        TTSVoice("edge_tts", "ko-KR-SunHiNeural", "Korean", "SunHi", "Female"),
        TTSVoice("edge_tts", "fr-FR-DeniseNeural", "French", "Denise", "Female"),
        TTSVoice("edge_tts", "es-ES-ElviraNeural", "Spanish", "Elvira", "Female"),
        TTSVoice("edge_tts", "zh-CN-XiaoxiaoNeural", "Chinese", "Xiaoxiao", "Female"),
    ],
    "google_cloud": [
        TTSVoice(
            "google_cloud", "en-US-Neural2-C", "English (US)", "Neural2-C", "Female"
        ),
        TTSVoice(
            "google_cloud", "en-US-Neural2-D", "English (US)", "Neural2-D", "Male"
        ),
        TTSVoice(
            "google_cloud", "vi-VN-Neural2-A", "Vietnamese", "Neural2-A", "Female"
        ),
        TTSVoice(
            "google_cloud", "vi-VN-Standard-B", "Vietnamese", "Standard-B", "Male"
        ),
        TTSVoice("google_cloud", "ja-JP-Neural2-B", "Japanese", "Neural2-B", "Female"),
    ],
    "openai": [
        TTSVoice("openai", "alloy", "English", "Alloy", "Neutral", "gpt-4o-mini-tts"),
        TTSVoice("openai", "echo", "English", "Echo", "Male", "gpt-4o-mini-tts"),
        TTSVoice("openai", "fable", "English", "Fable", "Neutral", "gpt-4o-mini-tts"),
        TTSVoice("openai", "onyx", "English", "Onyx", "Male", "gpt-4o-mini-tts"),
        TTSVoice("openai", "nova", "English", "Nova", "Female", "gpt-4o-mini-tts"),
        TTSVoice(
            "openai", "shimmer", "English", "Shimmer", "Female", "gpt-4o-mini-tts"
        ),
    ],
}


def providers_for(kind: str) -> list[str]:
    """Return the provider names offered for a generation ``kind`` (text/image → LLM, tts → TTS)."""
    return list(TTS_PROVIDERS) if kind == KIND_TTS else list(LLM_PROVIDERS)


def text_models(provider: str) -> list[str]:
    """Return the curated text-model ids for ``provider`` (empty if unknown)."""
    return list(_TEXT_MODELS.get(provider, []))


def image_models(provider: str) -> list[str]:
    """Return the curated image-model ids for ``provider`` (empty if unknown)."""
    return list(_IMAGE_MODELS.get(provider, []))


def models_for(provider: str, kind: str) -> list[str]:
    """Return the model ids for ``provider`` under ``kind`` (image list for image, else text)."""
    return image_models(provider) if kind == KIND_IMAGE else text_models(provider)


def voices_for(provider: str) -> list[TTSVoice]:
    """Return the curated voices for a TTS ``provider`` (empty when not enumerable offline)."""
    return list(_VOICES.get(provider, []))


def catalog_payload() -> dict[str, object]:
    """Build the JSON-able catalog the Smart Notes page bakes in to drive its dropdowns.

    Shape::

        {
          "llm_providers": [...],            # for text/image kinds
          "tts_providers": [...],            # for the tts kind
          "text_models":  {provider: [id, ...]},
          "image_models": {provider: [id, ...]},
          "voices":       {provider: [{"voice", "label", "language", "gender", "model"}, ...]},
        }

    The page reads ``llm_providers``/``tts_providers`` to fill the Provider dropdown by kind,
    ``text_models``/``image_models`` to fill the Model dropdown, and ``voices`` to fill the
    Voice dropdown for sound fields.
    """
    return {
        "llm_providers": list(LLM_PROVIDERS),
        "tts_providers": list(TTS_PROVIDERS),
        "languages": [dict(lang) for lang in LANGUAGES],
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
            for provider, voices in _VOICES.items()
        },
    }
