"""Piper TTS — local, offline, open-source (WAV output).

Piper is a CPU-friendly neural TTS that runs fully offline from an ONNX voice model. It's a
native binary (not a pure-Python lib), so the subprocess call is isolated behind the
injectable :class:`PiperRunner` (DIP) — the default :class:`SubprocessPiperRunner` shells out
to the ``piper`` binary, and tests inject a fake. The user supplies the binary (on PATH or a
configured path) and a voice ``model`` (.onnx).
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider


class PiperRunner(ABC):
    """Transport that turns (text, model_path) into WAV bytes."""

    @abstractmethod
    def run(self, text: str, model_path: str) -> bytes:
        """Return WAV audio for ``text`` using the voice at ``model_path``."""


class SubprocessPiperRunner(PiperRunner):
    """Default transport shelling out to the ``piper`` CLI binary."""

    def __init__(self, binary: str = "piper") -> None:
        self._binary = binary

    def run(self, text: str, model_path: str) -> bytes:
        if not model_path:
            raise ProviderError("piper requires 'model' (path to a .onnx voice file)")
        try:
            proc = subprocess.run(
                [self._binary, "--model", model_path, "--output_file", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError(f"piper synthesis failed: {exc}") from exc
        return proc.stdout


class PiperTTS(TTSProvider):
    """Synthesises speech offline via Piper (WAV)."""

    name = "piper"
    audio_ext = "wav"
    requires_api = False  # offline, open-source; needs the piper binary, not a key

    def __init__(
        self,
        model: str = "",
        binary: str = "piper",
        runner: Optional[PiperRunner] = None,
    ) -> None:
        self._model = model
        self._runner = runner or SubprocessPiperRunner(binary)

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        return self._runner.run(text, voice or self._model)
