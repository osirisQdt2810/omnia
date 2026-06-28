"""OpenAI-compatible TTS provider (``/audio/speech``)."""

from __future__ import annotations

from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.tts.base import TTSProvider


class OpenAICompatibleTTS(TTSProvider):
    """Talks to any ``/audio/speech`` compatible API (OpenAI, etc.)."""

    name = "openai_compatible"
    audio_ext = "mp3"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        http: Optional[HttpClient] = None,
    ) -> None:
        if not api_key:
            raise ProviderError("OpenAI-compatible TTS requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._http = http or DEFAULT_HTTP_CLIENT

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        payload = {
            "model": self._model,
            "input": text,
            "voice": voice or self._voice,
            "response_format": "mp3",
        }
        return self._http.post_json_for_bytes(
            f"{self._base_url}/audio/speech",
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
