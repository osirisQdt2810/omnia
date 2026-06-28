"""Free, key-free TTS via the Google Translate speech endpoint (gTTS-style).

No API key required — the default voice provider so smart-notes works out of the box. Long
text is split into <=200-char chunks (a Translate limit) at word boundaries and the MP3
fragments are concatenated.
"""

from __future__ import annotations

from typing import Optional

from omnia.core.providers.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.tts.base import TTSProvider

_ENDPOINT = "https://translate.google.com/translate_tts"
_MAX_CHARS = 200
# Translate returns 403 without a browser-like User-Agent.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def split_text(text: str, max_chars: int = _MAX_CHARS) -> list[str]:
    """Split ``text`` into chunks of at most ``max_chars``, breaking on whitespace.

    A single word longer than ``max_chars`` is hard-split. Returns ``[]`` for blank text.
    """
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current = ""
    for word in words:
        while len(word) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:max_chars])
            word = word[max_chars:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


class GoogleTranslateTTS(TTSProvider):
    """Key-free TTS using translate.google.com."""

    name = "google_translate"
    audio_ext = "mp3"
    requires_api = False  # free, no key

    def __init__(
        self,
        lang: str = "en",
        tld: str = "com",
        http: Optional[HttpClient] = None,
    ) -> None:
        self._lang = lang
        self._tld = tld
        self._http = http or DEFAULT_HTTP_CLIENT

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        use_lang = lang or self._lang
        parts = split_text(text)
        endpoint = (
            _ENDPOINT.replace("translate.google.com", f"translate.google.{self._tld}")
            if self._tld != "com"
            else _ENDPOINT
        )
        audio = bytearray()
        total = len(parts)
        for idx, part in enumerate(parts):
            params = {
                "ie": "UTF-8",
                "q": part,
                "tl": use_lang,
                "total": str(total),
                "idx": str(idx),
                "textlen": str(len(part)),
                "client": "tw-ob",
            }
            audio += self._http.get_bytes(
                endpoint, params=params, headers={"User-Agent": _USER_AGENT}
            )
        return bytes(audio)
