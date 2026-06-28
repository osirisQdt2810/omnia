"""Microsoft Edge neural TTS — free, no API key, strong quality (incl. Vietnamese).

Edge TTS speaks over a WebSocket protocol, so it can't use the stdlib HTTP client. The
network transport is isolated behind the injectable :class:`EdgeSynthesizer` (DIP) — the
default :class:`EdgeLibSynthesizer` delegates to the maintained ``edge-tts`` library (lazy
import, clear error if absent), and tests inject a fake. This keeps the provider SOLID and
sweepable without reimplementing a fragile reverse-engineered protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider

_DEFAULT_VOICES = {"en": "en-US-AriaNeural", "vi": "vi-VN-HoaiMyNeural"}


class EdgeSynthesizer(ABC):
    """Transport that turns (text, voice) into MP3 bytes via Edge TTS."""

    @abstractmethod
    def synthesize(self, text: str, voice: str) -> bytes:
        """Return MP3 audio for ``text`` spoken by ``voice``."""


class EdgeLibSynthesizer(EdgeSynthesizer):
    """Default transport delegating to the ``edge-tts`` library."""

    def synthesize(self, text: str, voice: str) -> bytes:
        try:
            import edge_tts
        except ImportError as exc:  # pragma: no cover - exercised only without the lib
            raise ProviderError(
                "edge_tts needs the 'edge-tts' package (pip install edge-tts), or pick "
                "another TTS provider (google_translate is free and needs nothing)."
            ) from exc
        import asyncio

        audio = bytearray()

        async def _run() -> None:
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    audio.extend(chunk["data"])

        try:
            asyncio.run(_run())
        except Exception as exc:  # network / protocol failure
            raise ProviderError(
                f"edge_tts synthesis failed (voice={voice}): {exc}"
            ) from exc
        return bytes(audio)


class EdgeTTS(TTSProvider):
    """Synthesises speech via Microsoft Edge neural voices (MP3)."""

    name = "edge_tts"
    audio_ext = "mp3"
    requires_api = False  # free Microsoft Edge voices, no key

    def __init__(
        self,
        lang: str = "en",
        voice: str = "",
        synthesizer: Optional[EdgeSynthesizer] = None,
    ) -> None:
        self._lang = lang
        self._voice = voice
        self._synthesizer = synthesizer or EdgeLibSynthesizer()

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        chosen = voice or self._voice or self._default_voice(lang)
        return self._synthesizer.synthesize(text, chosen)

    def _default_voice(self, lang: Optional[str]) -> str:
        key = (lang or self._lang or "en").strip().lower()
        return _DEFAULT_VOICES.get(key, _DEFAULT_VOICES["en"])
