"""TTS provider interface (adapted from vio-ai's ``BaseTTSProvider``).

Pure module — no Anki imports. ``synthesize`` returns the audio bytes; the caller writes
them into the collection's media folder.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class TTSProvider(ABC):
    """Synthesises speech audio from text."""

    name: str = ""
    # Container/extension of the bytes returned by :meth:`synthesize`.
    audio_ext: str = "mp3"
    # Whether this provider needs an API key / cloud credentials to call. False for keyless /
    # offline / open-source providers (google_translate, edge_tts, piper) that must run without
    # any secret. Used to classify providers and to derive test markers.
    requires_api: bool = True

    @abstractmethod
    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        """Return audio bytes (in :attr:`audio_ext` format) speaking ``text``.

        Args:
            text: The text to speak.
            lang: BCP-47-ish language code (e.g. ``"en"``, ``"vi"``); provider-specific.
            voice: Provider-specific voice id (ignored by single-voice providers).

        Raises:
            ProviderError: On bad config or an HTTP/network failure.
        """
