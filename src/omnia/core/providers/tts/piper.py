"""Piper TTS — local, offline, open-source (WAV output).

Piper is a CPU-friendly neural TTS that runs fully offline from an ONNX voice model. The model
itself is plain data and ships in the add-on under ``src/omnia/models/piper/`` (a ``<voice>.onnx``
+ ``<voice>.onnx.json`` pair); a sound field's "voice" is either a bundled voice NAME
(resolved to that dir) or an absolute ``.onnx`` path. The runtime, however, is the
``piper-tts`` package, which wraps **native** ``onnxruntime`` — a compiled, platform-specific
wheel that cannot be vendored cross-platform and so is NOT shipped.

Per ADR-005 the add-on **manages** piper in a per-provider sidecar venv (the native
``onnxruntime`` ABI matches the venv interpreter by construction). Synthesis goes through the
injectable :class:`PiperRunner` seam (DIP): the default :class:`SidecarPiperRunner` runs
piper's CLI in that managed venv via the :class:`NativeRuntimeManager` — text in on stdin,
WAV out to a temp file — and raises a clear "enable it in Advanced" :class:`ProviderError`
when the runtime isn't installed. The legacy in-process :class:`PiperVoiceRunner` (which uses
a directly-importable ``piper`` package) stays available as an injectable alternative; a test
can inject a fake runner.
"""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.native_runtime import (
    NativeRuntimeManager,
    NativeRuntimeSpec,
    default_manager,
    register_native_runtime,
)
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import register_tts

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# Bundled voice models live next to the add-on package: src/omnia/models/piper/<voice>.onnx
# (piper.py is at src/omnia/core/providers/tts/piper.py → parents[3] is src/omnia).
_MODELS_DIR = Path(__file__).resolve().parents[3] / "models" / "piper"
_DEFAULT_VOICE = "vi_VN-vais1000-medium"  # the bundled Vietnamese voice

# The managed-venv spec (ADR-005): a one-shot CLI run in the venv via piper's console script.
# Per-call args (``-m <model> -f <output>``) are appended as ``extra_argv`` by the runner.
SPEC: NativeRuntimeSpec = register_native_runtime(
    NativeRuntimeSpec(
        name="piper",
        section="tts",
        label="Piper (offline neural, local)",
        pip_packages=("piper-tts",),
        mode="cli",
        size_hint="~50 MB",
        cli_argv=("{bin}/piper",),
    )
)


class PiperRunner(ABC):
    """Transport that turns (text, model_path) into WAV bytes."""

    @abstractmethod
    def run(self, text: str, model_path: str) -> bytes:
        """Return WAV audio for ``text`` using the voice model at ``model_path``."""


class SidecarPiperRunner(PiperRunner):
    """Default runner: synthesizes by running piper's CLI in the add-on-managed venv (ADR-005).

    Feeds ``text`` on stdin and has piper write WAV to a temp ``--output_file``, then reads
    those bytes back. The native ``onnxruntime`` lives in the managed venv, isolated from
    Anki's interpreter. Raises a clear "enable it in Advanced" error when the runtime is not
    installed (the manager does not auto-install — installing is an explicit user toggle).
    """

    def __init__(self, manager: Optional[NativeRuntimeManager] = None) -> None:
        # Inject for tests; default to the process-wide manager (lazy — no Anki import here).
        self._manager = manager

    def run(self, text: str, model_path: str) -> bytes:
        if not os.path.isfile(model_path):
            raise ProviderError(
                f"piper voice model not found at {model_path}. Put a <voice>.onnx (+ .onnx.json) "
                "in the add-on's models/piper/ dir, e.g. `python -m piper.download_voices "
                "vi_VN-vais1000-medium --data-dir <addon>/models/piper`."
            )
        manager = self._manager or default_manager()
        # piper writes the WAV to a file rather than stdout, so use a temp output path and read
        # the bytes back; text goes in on stdin.
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.wav"
            code = manager.run_in_venv(
                SPEC,
                ["-m", model_path, "-f", str(out_path)],
                input=text.encode("utf-8"),
            )
            if code != 0 or not out_path.exists():
                raise ProviderError(
                    f"piper synthesis failed (model={model_path}, exit={code})."
                )
            return out_path.read_bytes()


class PiperVoiceRunner(PiperRunner):
    """Alternative runner: synthesizes via a directly-importable ``piper`` package, in-process.

    ``piper-tts`` wraps native ``onnxruntime`` (a compiled wheel), so it can't ship with the
    add-on; a missing install raises a clear, actionable error instead of crashing. Lazily
    imported so this module loads on a stock Anki without ``piper-tts`` present. Kept as an
    injectable alternative to the default managed-venv :class:`SidecarPiperRunner`.
    """

    def run(self, text: str, model_path: str) -> bytes:
        import io
        import wave

        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise ProviderError(
                "piper requires the 'piper-tts' package (it wraps native onnxruntime, so it "
                "can't ship with the add-on). Run `pip install piper-tts`, or pick edge_tts / "
                "google_translate (free, nothing to install)."
            ) from exc
        if not os.path.isfile(model_path):
            raise ProviderError(
                f"piper voice model not found at {model_path}. Put a <voice>.onnx (+ .onnx.json) "
                "in the add-on's models/piper/ dir, e.g. `python -m piper.download_voices "
                "vi_VN-vais1000-medium --data-dir <addon>/models/piper`."
            )
        # Boundary: surface any piper/onnx failure as a ProviderError.
        try:
            voice = PiperVoice.load(model_path)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                voice.synthesize_wav(text, wav)
            return buf.getvalue()
        except Exception as exc:
            raise ProviderError(
                f"piper synthesis failed (model={model_path}): {exc}"
            ) from exc


@register_tts("piper")
class PiperTTS(TTSProvider):
    """Synthesises speech offline via Piper (WAV) through an injected :class:`PiperRunner`."""

    name = "piper"
    audio_ext = "wav"
    # offline, open-source; needs the native piper-tts package, not a key
    requires_api = False
    # The bundled voices (their .onnx ships under models/piper/). A field's voice is the NAME
    # here; other voices can be dropped into models/piper/ and typed in the picker.
    CURATED_VOICES: ClassVar[list[TTSVoice]] = [
        TTSVoice("piper", _DEFAULT_VOICE, "Vietnamese", "vais1000", "Female", "", "vi"),
    ]

    def __init__(
        self,
        model: str = "",
        runner: Optional[PiperRunner] = None,
    ) -> None:
        self._model = model
        self._runner = runner or SidecarPiperRunner()

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> PiperTTS:
        return cls(model=config.get("model", ""))

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        return self._runner.run(text, self._resolve_model_path(voice))

    def _resolve_model_path(self, voice: Optional[str]) -> str:
        """Resolve a voice NAME or path to an ``.onnx`` model file.

        A bundled voice name (e.g. ``"vi_VN-vais1000-medium"``) resolves to
        ``models/piper/<name>.onnx``; a relative ``.onnx`` path resolves under that dir; an
        absolute ``.onnx`` path is used as-is. Falls back to the bundled default voice.
        """
        name = (voice or self._model or _DEFAULT_VOICE).strip()
        if not name:
            raise ProviderError(
                "piper needs a voice — a bundled voice name or a path to a .onnx file"
            )
        path = Path(name)
        if path.suffix != ".onnx":
            path = _MODELS_DIR / f"{name}.onnx"
        elif not path.is_absolute():
            path = _MODELS_DIR / path
        return str(path)
