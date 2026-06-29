"""TTS provider interface (adapted from vio-ai's ``BaseTTSProvider``).

Pure module — no Anki imports. ``synthesize`` returns the audio bytes; the caller writes
them into the collection's media folder. :class:`TTSVoice` lives here (next to the provider
interface) so each provider declares its own curated voices and ``list_voices`` is
polymorphic — the GUI/catalog read the aggregated set without importing any concrete provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient


@dataclass(frozen=True)
class TTSVoice:
    """One selectable TTS voice: what to pass to ``synthesize`` + how to label it.

    ``voice`` is the exact id handed to :meth:`TTSProvider.synthesize`; ``model`` is the TTS
    model where the provider needs one (e.g. OpenAI's ``gpt-4o-mini-tts``) and empty
    otherwise. ``language``/``name``/``gender`` are display metadata — together they form the
    human label so the user reads "Vietnamese · HoaiMy · Female" instead of a raw voice id.
    ``lang_code`` is the ISO 639-1 code (e.g. ``"vi"``) the global Auto-detect map groups by.
    """

    provider: str
    voice: str
    language: str
    name: str
    gender: str
    model: str = ""
    lang_code: str = ""

    @property
    def label(self) -> str:
        """Human label shown in the dropdown: ``<language> · <name> · <gender>``."""
        return f"{self.language} · {self.name} · {self.gender}"


class TTSProvider(ABC):
    """Synthesises speech audio from text."""

    name: str = ""
    # Container/extension of the bytes returned by :meth:`synthesize`.
    audio_ext: str = "mp3"
    # Whether this provider needs an API key / cloud credentials to call. False for keyless /
    # offline / open-source providers (google_translate, edge_tts, piper) that must run without
    # any secret. Used to classify providers and to derive test markers.
    requires_api: bool = True
    # The provider's own curated voices (the offline seed). Empty for providers whose voices
    # can't be enumerated offline (google_translate is language-only; piper is a local .onnx
    # path). Subclasses override this; :meth:`list_voices` reads it.
    CURATED_VOICES: ClassVar[list[TTSVoice]] = []

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> TTSProvider:
        """Build a configured instance from a ``config`` dict (the factory entry point).

        Each provider reads the keys it needs out of ``config`` and (where it does HTTP) wires
        in the injected ``http`` client. Subclasses MUST override this; the base raises so a
        provider that forgets to implement it fails loudly rather than mis-building.

        Args:
            config: The provider's config subsection (already includes ``provider``).
            http: Optional HTTP client to inject.

        Returns:
            A ready-to-use provider instance.

        Raises:
            NotImplementedError: If the subclass does not override this.
        """
        raise NotImplementedError(f"{cls.__name__} must implement from_config()")

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

    @classmethod
    def list_voices(
        cls, http: Optional[HttpClient] = None, *, refresh: bool = False
    ) -> list[TTSVoice]:
        """Return the provider's selectable voices.

        A classmethod (not an instance method) so the catalog/GUI can enumerate voices WITHOUT
        building a configured provider (some need an api_key / token to construct). The default
        returns the curated seed; a provider that can fetch a live list (e.g. edge_tts)
        overrides this to fetch when ``refresh`` is set and ``http`` is available, merging the
        fetched voices over the seed and caching them. Offline-safe: without ``refresh`` (or
        with no ``http``) it returns the curated seed and never touches the network.

        Args:
            http: HTTP client for a live fetch (only used by fetch-capable providers).
            refresh: Fetch + cache the live list when True; else return the curated seed.

        Returns:
            The provider's voices.
        """
        return list(cls.CURATED_VOICES)
