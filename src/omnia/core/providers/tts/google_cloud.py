"""Google Cloud Text-to-Speech provider (REST).

High-quality neural voices. Authenticates with a :class:`TokenSource` — the *same* Google
service-account / gcloud / token auth as gemini_vertex — so a user already set up for Vertex
gets Cloud TTS for free. Stdlib HTTP only (no google-cloud SDK): POSTs ``text:synthesize``
and decodes the base64 ``audioContent``.
"""

from __future__ import annotations

import base64
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.http import DEFAULT_HTTP_CLIENT, HttpClient
from omnia.core.providers.token_source import TokenSource
from omnia.core.providers.tts.base import TTSProvider

_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
# Logical language -> BCP-47 code used by Cloud TTS.
_LANG_CODES = {"en": "en-US", "vi": "vi-VN"}


class GoogleCloudTTS(TTSProvider):
    """Synthesises speech via Google Cloud Text-to-Speech (MP3)."""

    name = "google_cloud"
    audio_ext = "mp3"

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

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        voice_params: dict[str, object] = {"languageCode": self._resolve_lang(lang)}
        chosen = voice or self._voice
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

    def _resolve_lang(self, lang: Optional[str]) -> str:
        if self._language_code:
            return self._language_code
        key = (lang or self._lang or "en").strip().lower()
        return _LANG_CODES.get(key, key if "-" in key else "en-US")
