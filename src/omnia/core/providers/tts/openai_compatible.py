"""OpenAI-compatible TTS provider (``/audio/speech``)."""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import register_tts

# Default base URL per config name — the openai family is ONE class under three names that
# differ only by where they point. ``from_config`` picks the URL by ``config['provider']``.
_OPENAI_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "https://api.openai.com/v1",
}


@register_tts("openai", "openrouter", "openai_compatible")
class OpenAICompatibleTTS(TTSProvider):
    """Talks to any ``/audio/speech`` compatible API (OpenAI, etc.)."""

    name = "openai_compatible"
    audio_ext = "mp3"
    # The OpenAI TTS voices (English-labelled today). Tagged ``provider="openai"`` so the
    # Auto-detect mapping reads ``openai:alloy`` (the picker offers them under the openai name).
    CURATED_VOICES: ClassVar[list[TTSVoice]] = [
        TTSVoice(
            "openai", "alloy", "English", "Alloy", "Neutral", "gpt-4o-mini-tts", "en"
        ),
        TTSVoice("openai", "echo", "English", "Echo", "Male", "gpt-4o-mini-tts", "en"),
        TTSVoice(
            "openai", "fable", "English", "Fable", "Neutral", "gpt-4o-mini-tts", "en"
        ),
        TTSVoice("openai", "onyx", "English", "Onyx", "Male", "gpt-4o-mini-tts", "en"),
        TTSVoice(
            "openai", "nova", "English", "Nova", "Female", "gpt-4o-mini-tts", "en"
        ),
        TTSVoice(
            "openai", "shimmer", "English", "Shimmer", "Female", "gpt-4o-mini-tts", "en"
        ),
    ]

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        http: Optional[HttpClient] = None,
        *,
        response_format: str = "mp3",
    ) -> None:
        if not api_key:
            raise ProviderError("OpenAI-compatible TTS requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._response_format = response_format
        self._http = http or DEFAULT_HTTP_CLIENT

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> OpenAICompatibleTTS:
        # The openai family shares this class under three names; the default base URL depends
        # on which name was selected (config['provider']).
        provider = config.get("provider", "openai_compatible")
        base_url = config.get("base_url") or _OPENAI_DEFAULTS.get(
            provider, _OPENAI_DEFAULTS["openai"]
        )
        return cls(
            api_key=config.get("api_key", ""),
            base_url=base_url,
            model=config.get("model") or "gpt-4o-mini-tts",
            voice=config.get("voice") or "alloy",
            http=http,
        )

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        payload = {
            "model": self._model,
            "input": text,
            "voice": voice or self._voice,
            "response_format": self._response_format,
        }
        return self._http.post_json_for_bytes(
            f"{self._base_url}/audio/speech",
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
