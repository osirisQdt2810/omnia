"""Piper TTS — local, offline, open-source (WAV output).

Piper is a CPU-friendly neural TTS that runs fully offline from an ONNX voice model. It's a
native binary (not a pure-Python lib), so it can't be vendored cross-platform and the add-on
**does not shell out** to it (Anki can't rely on a CLI/binary on PATH). The transport is
isolated behind the injectable :class:`PiperRunner` (DIP): the default runner raises a clear
:class:`ProviderError`, and a future VENDORED native runner — or a test fake — can be injected
in its place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider


class PiperRunner(ABC):
    """Transport that turns (text, model_path) into WAV bytes."""

    @abstractmethod
    def run(self, text: str, model_path: str) -> bytes:
        """Return WAV audio for ``text`` using the voice at ``model_path``."""


class UnavailablePiperRunner(PiperRunner):
    """Default runner: piper isn't bundled and the add-on never shells out, so it raises.

    Keeps the seam open (inject a vendored native runner or a fake) while ensuring the
    out-of-the-box path fails clearly instead of silently invoking a CLI.
    """

    def run(self, text: str, model_path: str) -> bytes:
        raise ProviderError(
            "piper requires a vendored native binary, which is not bundled — "
            "pick google_translate/edge_tts, or inject a PiperRunner"
        )


class PiperTTS(TTSProvider):
    """Synthesises speech offline via Piper (WAV) through an injected :class:`PiperRunner`."""

    name = "piper"
    audio_ext = "wav"
    requires_api = False  # offline, open-source; needs a native runner, not a key

    def __init__(
        self,
        model: str = "",
        runner: Optional[PiperRunner] = None,
    ) -> None:
        self._model = model
        self._runner = runner or UnavailablePiperRunner()

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        model_path = voice or self._model
        if not model_path:
            raise ProviderError("piper requires 'model' (path to a .onnx voice file)")
        return self._runner.run(text, model_path)
