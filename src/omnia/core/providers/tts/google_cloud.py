"""Google Cloud Text-to-Speech provider (REST).

High-quality neural voices. Authenticates with a :class:`TokenSource` — the *same* Google
service-account / access-token auth as gemini_vertex — so a user already set up for Vertex
gets Cloud TTS for free. Stdlib HTTP only (no google-cloud SDK): POSTs ``text:synthesize``
and decodes the base64 ``audioContent``.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar, Optional

from omnia.core.network.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.token_source import TokenSource, resolve_token_source
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import register_tts

_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
# Logical language -> BCP-47 code used by Cloud TTS.
_LANG_CODES = {"en": "en-US", "vi": "vi-VN"}


def _language_code_from_voice(voice: str) -> str:
    """Return the BCP-47 language a Google voice name encodes ("vi-VN-Neural2-A" → "vi-VN").

    Google Cloud voice names always start with ``<lang>-<REGION>``; that prefix IS the voice's
    language. Returns "" when ``voice`` isn't in that shape (so the caller falls back).
    """
    parts = (voice or "").split("-")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}-{parts[1]}"
    return ""


@register_tts("google_cloud")
class GoogleCloudTTS(TTSProvider):
    """Synthesises speech via Google Cloud Text-to-Speech (MP3)."""

    name = "google_cloud"
    audio_ext = "mp3"
    # A small curated seed (the full list needs an authenticated voices.list call). Each carries
    # its ISO 639-1 lang_code so the Auto-detect editor groups it.
    CURATED_VOICES: ClassVar[list[TTSVoice]] = [
        TTSVoice(
            "google_cloud",
            "en-US-Neural2-C",
            "English (US)",
            "Neural2-C",
            "Female",
            "",
            "en",
        ),
        TTSVoice(
            "google_cloud",
            "en-US-Neural2-D",
            "English (US)",
            "Neural2-D",
            "Male",
            "",
            "en",
        ),
        TTSVoice(
            "google_cloud",
            "vi-VN-Neural2-A",
            "Vietnamese",
            "Neural2-A",
            "Female",
            "",
            "vi",
        ),
        TTSVoice(
            "google_cloud",
            "vi-VN-Standard-B",
            "Vietnamese",
            "Standard-B",
            "Male",
            "",
            "vi",
        ),
        TTSVoice(
            "google_cloud",
            "ja-JP-Neural2-B",
            "Japanese",
            "Neural2-B",
            "Female",
            "",
            "ja",
        ),
    ]

    def __init__(
        self,
        token_source: TokenSource,
        lang: str = "en",
        voice: str = "",
        language_code: str = "",
        speaking_rate: float = 1.0,
        http: Optional[HttpClient] = None,
    ) -> None:
        self._token_source = token_source
        self._lang = lang
        self._voice = voice
        self._language_code = language_code
        self._speaking_rate = speaking_rate
        self._http = http or DEFAULT_HTTP_CLIENT

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> GoogleCloudTTS:
        # Reuses the same Google auth strategies as gemini_vertex (the ProviderHub merges the
        # Vertex auth fields into this config when the active TTS provider is google_cloud).
        token_source = resolve_token_source(config, http or DEFAULT_HTTP_CLIENT)
        return cls(
            token_source=token_source,
            lang=config.get("lang", "en"),
            voice=config.get("voice", ""),
            language_code=config.get("language_code", ""),
            speaking_rate=float(config.get("speaking_rate", 1.0)),
            http=http,
        )

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        chosen = voice or self._voice
        voice_params: dict[str, object] = {
            "languageCode": self._resolve_lang(lang, chosen)
        }
        if chosen:
            voice_params["name"] = chosen
        payload = {
            "input": {"text": text},
            "voice": voice_params,
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": self._speaking_rate,
            },
        }
        headers = {"Authorization": f"Bearer {self._token_source.token()}"}
        resp = self._http.post_json(_ENDPOINT, payload, headers=headers)
        encoded = resp.get("audioContent")
        if not encoded:
            raise ProviderError(f"Google Cloud TTS returned no audioContent: {resp}")
        return base64.b64decode(encoded)

    def _resolve_lang(self, lang: Optional[str], voice: str = "") -> str:
        # A specific voice name encodes its own language; Google rejects a languageCode that
        # disagrees with the chosen voice (HTTP 400 "language code 'en-US' doesn't match the
        # voice 'vi-VN-Neural2-A'"), so the voice's own language wins over a configured/detected
        # lang. Only without a voice do the explicit override / mapping apply.
        from_voice = _language_code_from_voice(voice)
        if from_voice:
            return from_voice
        if self._language_code:
            return self._language_code
        key = (lang or self._lang or "en").strip().lower()
        # An unknown bare 2-letter code derives a plausible BCP-47 (e.g. "fr" -> "fr-FR")
        # rather than silently defaulting to en-US, which would speak the WRONG language. An
        # already-BCP-47 key (with a "-") passes through; known codes use the curated map.
        return _LANG_CODES.get(key, key if "-" in key else f"{key}-{key.upper()}")
