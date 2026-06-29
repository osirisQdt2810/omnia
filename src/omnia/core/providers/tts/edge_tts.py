"""Microsoft Edge neural TTS — free, no API key, strong quality (incl. Vietnamese).

Edge TTS is Microsoft's **online** neural TTS service (free + keyless, but a proprietary cloud
service — not an offline model): the client streams text to Microsoft over a WebSocket and gets
MP3 back. The maintained ``edge-tts`` PyPI package speaks this protocol on top of ``aiohttp``,
which ships **compiled** extensions and so cannot be vendored into a cross-platform, pure-Python
Anki add-on. :class:`EdgeProtocolSynthesizer` therefore speaks the protocol directly over the
stdlib :class:`~omnia.core.network.websocket.WebSocketClient` (the ``Sec-MS-GEC`` DRM token,
the ``speech.config``/``ssml`` messages, and the binary audio framing all mirror ``edge-tts`` so
it stays compatible). Result: Edge voices work in shipped Anki with nothing to install.

The transport is injectable behind the :class:`EdgeSynthesizer` ABC (DIP) so :class:`EdgeTTS`
stays SOLID and tests can inject a fake without touching the network. Because it targets a
reverse-engineered Microsoft service, the constants below may need bumping if Microsoft changes
the endpoint — that is the only maintenance cost of shipping it pure-Python.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Optional
from xml.sax.saxutils import escape

from omnia.core.network.websocket import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_TEXT,
    WebSocketClient,
    WebSocketError,
)
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import register_tts

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

_DEFAULT_VOICES = {"en": "en-US-AriaNeural", "vi": "vi-VN-HoaiMyNeural"}

# One real female Azure/Edge neural voice per supported language (the offline seed). A
# slightly-off seed id is acceptable — Refresh (the voices/list manifest) corrects it and the
# user re-picks. Each carries its ISO 639-1 lang_code so the Auto-detect editor groups it.
CURATED_VOICES: list[TTSVoice] = [
    TTSVoice("edge_tts", "ar-EG-SalmaNeural", "Arabic", "Salma", "Female", "", "ar"),
    TTSVoice(
        "edge_tts", "bn-BD-NabanitaNeural", "Bengali", "Nabanita", "Female", "", "bn"
    ),
    TTSVoice(
        "edge_tts", "bg-BG-KalinaNeural", "Bulgarian", "Kalina", "Female", "", "bg"
    ),
    TTSVoice("edge_tts", "ca-ES-JoanaNeural", "Catalan", "Joana", "Female", "", "ca"),
    TTSVoice(
        "edge_tts", "zh-CN-XiaoxiaoNeural", "Chinese", "Xiaoxiao", "Female", "", "zh"
    ),
    TTSVoice(
        "edge_tts", "hr-HR-GabrijelaNeural", "Croatian", "Gabrijela", "Female", "", "hr"
    ),
    TTSVoice("edge_tts", "cs-CZ-VlastaNeural", "Czech", "Vlasta", "Female", "", "cs"),
    TTSVoice(
        "edge_tts", "da-DK-ChristelNeural", "Danish", "Christel", "Female", "", "da"
    ),
    TTSVoice("edge_tts", "nl-NL-ColetteNeural", "Dutch", "Colette", "Female", "", "nl"),
    TTSVoice("edge_tts", "en-US-AriaNeural", "English", "Aria", "Female", "", "en"),
    TTSVoice("edge_tts", "fi-FI-NooraNeural", "Finnish", "Noora", "Female", "", "fi"),
    TTSVoice("edge_tts", "fr-FR-DeniseNeural", "French", "Denise", "Female", "", "fr"),
    TTSVoice("edge_tts", "de-DE-KatjaNeural", "German", "Katja", "Female", "", "de"),
    TTSVoice("edge_tts", "el-GR-AthinaNeural", "Greek", "Athina", "Female", "", "el"),
    TTSVoice("edge_tts", "he-IL-HilaNeural", "Hebrew", "Hila", "Female", "", "he"),
    TTSVoice("edge_tts", "hi-IN-SwaraNeural", "Hindi", "Swara", "Female", "", "hi"),
    TTSVoice("edge_tts", "hu-HU-NoemiNeural", "Hungarian", "Noemi", "Female", "", "hu"),
    TTSVoice(
        "edge_tts", "id-ID-GadisNeural", "Indonesian", "Gadis", "Female", "", "id"
    ),
    TTSVoice("edge_tts", "it-IT-ElsaNeural", "Italian", "Elsa", "Female", "", "it"),
    TTSVoice(
        "edge_tts", "ja-JP-NanamiNeural", "Japanese", "Nanami", "Female", "", "ja"
    ),
    TTSVoice("edge_tts", "ko-KR-SunHiNeural", "Korean", "SunHi", "Female", "", "ko"),
    TTSVoice("edge_tts", "ms-MY-YasminNeural", "Malay", "Yasmin", "Female", "", "ms"),
    TTSVoice(
        "edge_tts", "nb-NO-PernilleNeural", "Norwegian", "Pernille", "Female", "", "no"
    ),
    TTSVoice("edge_tts", "fa-IR-DilaraNeural", "Persian", "Dilara", "Female", "", "fa"),
    TTSVoice("edge_tts", "pl-PL-ZofiaNeural", "Polish", "Zofia", "Female", "", "pl"),
    TTSVoice(
        "edge_tts",
        "pt-BR-FranciscaNeural",
        "Portuguese",
        "Francisca",
        "Female",
        "",
        "pt",
    ),
    TTSVoice("edge_tts", "ro-RO-AlinaNeural", "Romanian", "Alina", "Female", "", "ro"),
    TTSVoice(
        "edge_tts", "ru-RU-SvetlanaNeural", "Russian", "Svetlana", "Female", "", "ru"
    ),
    TTSVoice(
        "edge_tts", "sk-SK-ViktoriaNeural", "Slovak", "Viktoria", "Female", "", "sk"
    ),
    TTSVoice("edge_tts", "es-ES-ElviraNeural", "Spanish", "Elvira", "Female", "", "es"),
    TTSVoice("edge_tts", "sv-SE-SofieNeural", "Swedish", "Sofie", "Female", "", "sv"),
    TTSVoice("edge_tts", "ta-IN-PallaviNeural", "Tamil", "Pallavi", "Female", "", "ta"),
    TTSVoice("edge_tts", "te-IN-ShrutiNeural", "Telugu", "Shruti", "Female", "", "te"),
    TTSVoice(
        "edge_tts", "th-TH-PremwadeeNeural", "Thai", "Premwadee", "Female", "", "th"
    ),
    TTSVoice("edge_tts", "tr-TR-EmelNeural", "Turkish", "Emel", "Female", "", "tr"),
    TTSVoice(
        "edge_tts", "uk-UA-PolinaNeural", "Ukrainian", "Polina", "Female", "", "uk"
    ),
    TTSVoice(
        "edge_tts", "vi-VN-HoaiMyNeural", "Vietnamese", "HoaiMy", "Female", "", "vi"
    ),
]

# Microsoft's public Edge "voices/list" manifest (keyless GET, the same trusted-client token
# the WebSocket synthesizer uses). Used by the GUI's Refresh-voices action to enumerate every
# current Edge neural voice instead of relying on the curated seed.
_VOICES_LIST_URL = (
    "https://speech.platform.bing.com/consumer/speech/synthesize/readaloud/voices/list"
    "?trustedclienttoken={token}"
)


class EdgeSynthesizer(ABC):
    """Transport that turns (text, voice) into MP3 bytes via Edge TTS."""

    @abstractmethod
    def synthesize(self, text: str, voice: str) -> bytes:
        """Return MP3 audio for ``text`` spoken by ``voice``."""


class EdgeProtocolSynthesizer(EdgeSynthesizer):
    """Default transport: speaks the Edge WebSocket protocol over pure stdlib (no deps).

    Self-contained — all the Edge-specific protocol (endpoint constants, the ``Sec-MS-GEC``
    DRM token, the two request messages, binary audio extraction, and long-text splitting)
    lives here; only the generic WebSocket transport is shared via the core client.
    """

    # Microsoft Edge endpoint constants (kept in sync with the edge-tts package).
    _TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
    _BASE_HOST = "speech.platform.bing.com"
    _BASE_PATH = "/consumer/speech/synthesize/readaloud/edge/v1"
    _CHROMIUM_FULL_VERSION = "143.0.3650.75"
    _OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
    _ORIGIN = "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold"

    _WIN_EPOCH = 11644473600  # seconds between 1601-01-01 and the Unix epoch
    _S_TO_NS = 1e9
    # Keep each SSML request well under the service size limit; longer text is split on
    # whitespace into requests whose MP3 outputs concatenate cleanly.
    _MAX_CHUNK_BYTES = 3000

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    # --- public transport ---------------------------------------------------------------
    def synthesize(self, text: str, voice: str) -> bytes:
        cleaned = (text or "").strip()
        if not cleaned:
            return b""
        audio = bytearray()
        try:
            for chunk in self._split_on_whitespace(cleaned):
                audio += self._synthesize_chunk(chunk, voice)
        except WebSocketError as exc:
            raise ProviderError(
                f"edge_tts synthesis failed (voice={voice}): {exc}"
            ) from exc
        except OSError as exc:  # socket / TLS failure
            raise ProviderError(
                f"edge_tts synthesis failed (voice={voice}): {exc}"
            ) from exc
        if not audio:
            raise ProviderError(f"edge_tts returned no audio (voice={voice})")
        return bytes(audio)

    # --- protocol details ---------------------------------------------------------------
    def _synthesize_chunk(self, text: str, voice: str) -> bytes:
        connection_id = uuid.uuid4().hex
        url = (
            f"wss://{self._BASE_HOST}{self._BASE_PATH}"
            f"?TrustedClientToken={self._TRUSTED_CLIENT_TOKEN}"
            f"&ConnectionId={connection_id}"
            f"&Sec-MS-GEC={self._sec_ms_gec()}"
            f"&Sec-MS-GEC-Version=1-{self._CHROMIUM_FULL_VERSION}"
        )
        major = self._CHROMIUM_FULL_VERSION.split(".", 1)[0]
        headers = {
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Origin": self._ORIGIN,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": f"muid={uuid.uuid4().hex.upper()};",
        }
        ssml = self._mkssml(escape(self._remove_incompatible_characters(text)), voice)

        ws = WebSocketClient(url, headers, self._timeout)
        audio = bytearray()
        try:
            ws.send_text(self._speech_config_message())
            ws.send_text(self._ssml_message(ssml))
            while True:
                opcode, payload = ws.recv_message()
                if opcode == OPCODE_CLOSE:
                    break
                if opcode == OPCODE_BINARY:
                    audio += self._audio_from_binary_frame(payload)
                elif opcode == OPCODE_TEXT and b"Path:turn.end" in payload:
                    break
        finally:
            ws.close()
        return bytes(audio)

    def _sec_ms_gec(self) -> str:
        """The ``Sec-MS-GEC`` DRM token for the current 5-minute window.

        SHA-256 of ``"{windows_filetime_ticks}{trusted_client_token}"``, ticks = current UTC
        time pushed to the Windows file-time epoch, floored to 5 minutes, in 100-ns units.
        """
        ticks = time.time() + self._WIN_EPOCH
        ticks -= ticks % 300  # floor to the nearest 5 minutes
        ticks *= self._S_TO_NS / 100  # seconds -> 100-nanosecond intervals
        raw = f"{ticks:.0f}{self._TRUSTED_CLIENT_TOKEN}".encode("ascii")
        return hashlib.sha256(raw).hexdigest().upper()

    def _speech_config_message(self) -> str:
        return (
            f"X-Timestamp:{self._date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            f'"outputFormat":"{self._OUTPUT_FORMAT}"'
            "}}}}\r\n"
        )

    def _ssml_message(self, ssml: str) -> str:
        return (
            f"X-RequestId:{uuid.uuid4().hex}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{self._date_to_string()}Z\r\n"  # trailing Z mirrors an Edge bug
            "Path:ssml\r\n\r\n"
            f"{ssml}"
        )

    @staticmethod
    def _mkssml(escaped_text: str, voice: str) -> str:
        return (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            "xml:lang='en-US'>"
            f"<voice name='{voice}'>"
            "<prosody pitch='+0Hz' rate='+0%' volume='+0%'>"
            f"{escaped_text}"
            "</prosody></voice></speak>"
        )

    @staticmethod
    def _date_to_string() -> str:
        return time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()
        )

    @staticmethod
    def _remove_incompatible_characters(text: str) -> str:
        """Replace control chars the service rejects (e.g. the vertical tab) with spaces."""
        out = []
        for char in text:
            code = ord(char)
            if (0 <= code <= 8) or (11 <= code <= 12) or (14 <= code <= 31):
                out.append(" ")
            else:
                out.append(char)
        return "".join(out)

    @staticmethod
    def _audio_from_binary_frame(frame: bytes) -> bytes:
        """Audio bytes from a binary message (2-byte header length + headers + audio).

        Returns ``b""`` for the terminating zero-data frame or any non-audio binary message.
        """
        if len(frame) < 2:
            return b""
        header_length = int.from_bytes(frame[:2], "big")
        if header_length > len(frame):
            return b""
        if b"Path:audio" not in frame[:header_length]:
            return b""
        return frame[header_length + 2 :]

    @classmethod
    def _split_on_whitespace(cls, text: str) -> list[str]:
        """Split ``text`` into pieces whose UTF-8 length is <= the chunk budget (prefer spaces)."""
        if len(text.encode("utf-8")) <= cls._MAX_CHUNK_BYTES:
            return [text]
        pieces: list[str] = []
        current = ""
        for word in text.split(" "):
            candidate = word if not current else f"{current} {word}"
            if len(candidate.encode("utf-8")) > cls._MAX_CHUNK_BYTES and current:
                pieces.append(current)
                current = word
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces or [text]


@register_tts("edge_tts")
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
        self._synthesizer = synthesizer or EdgeProtocolSynthesizer()

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> EdgeTTS:
        # Edge speaks its own WebSocket protocol, not the shared HttpClient — http is unused.
        return cls(lang=config.get("lang", "en"), voice=config.get("voice", ""))

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        chosen = voice or self._voice or self._default_voice(lang)
        return self._synthesizer.synthesize(text, chosen)

    def _default_voice(self, lang: Optional[str]) -> str:
        key = (lang or self._lang or "en").strip().lower()
        return _DEFAULT_VOICES.get(key, _DEFAULT_VOICES["en"])

    # The module-level seed, exposed as the class attribute (ClassVar — shared, not per-instance).
    CURATED_VOICES: ClassVar[list[TTSVoice]] = CURATED_VOICES

    @classmethod
    def list_voices(
        cls, http: Optional[HttpClient] = None, *, refresh: bool = False
    ) -> list[TTSVoice]:
        """Return the curated seed, or (when ``refresh``) the live manifest merged over it.

        Without ``refresh`` (or with no ``http``) returns the curated seed — offline-safe, no
        network. With ``refresh`` and an ``http`` client, fetches the keyless Edge
        ``voices/list`` manifest and MERGES the fetched voices over the seed (fetched entries
        win per voice id, seed-only voices are kept), so the dropdowns show the full set.

        Args:
            http: HTTP client for the live fetch (defaults to the process-wide stdlib client).
            refresh: Fetch + merge the live manifest when True.

        Returns:
            The provider's voices as :class:`TTSVoice`.

        Raises:
            ProviderError: On a malformed (non-array) response or a network failure (refresh).
        """
        if not refresh:
            return list(cls.CURATED_VOICES)
        fetched = cls._fetch_voices(http or _default_http())
        merged = {v.voice: v for v in cls.CURATED_VOICES}
        merged.update({v.voice: v for v in fetched})
        return list(merged.values())

    @classmethod
    def _fetch_voices(cls, client: HttpClient) -> list[TTSVoice]:
        """Fetch + normalize every current Edge neural voice from the public manifest."""
        url = _VOICES_LIST_URL.format(
            token=EdgeProtocolSynthesizer._TRUSTED_CLIENT_TOKEN
        )
        raw = client.get_bytes(url, headers={"Accept": "application/json"})
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                f"Invalid edge_tts voices/list response: {exc}"
            ) from exc
        if not isinstance(parsed, list):
            raise ProviderError(
                f"Expected a JSON array from edge_tts voices/list, got "
                f"{type(parsed).__name__}"
            )
        voices: list[TTSVoice] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            short_name = str(entry.get("ShortName", "")).strip()
            if not short_name:
                continue
            locale = str(entry.get("Locale", "")).strip()
            voices.append(
                TTSVoice(
                    provider="edge_tts",
                    voice=short_name,
                    language=locale,
                    name=str(entry.get("FriendlyName", "") or short_name),
                    gender=str(entry.get("Gender", "")),
                    lang_code=locale.split("-", 1)[0].lower(),
                )
            )
        return voices


def _default_http() -> HttpClient:
    """Return the process-wide default HTTP client (imported lazily — pure module top)."""
    from omnia.core.network.http import DEFAULT_HTTP_CLIENT

    return DEFAULT_HTTP_CLIENT
