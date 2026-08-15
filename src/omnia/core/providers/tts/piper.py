"""Piper TTS — local, offline, open-source (WAV output).

Piper is a CPU-friendly neural TTS that runs fully offline from an ONNX voice model. The model
is plain data (a ``<voice>.onnx`` + ``<voice>.onnx.json`` pair) but the weights are tens of MB,
so they are NOT packaged: they are fetched once, on first synthesis, into ``user_files`` by the
:class:`~omnia.core.providers.tts.voice_models.PiperVoiceStore` (which prefers an existing copy
— a previous download, or a developer's Git-LFS checkout under ``models/piper/``). A sound
field's "voice" is either a voice NAME (resolved through that store) or an absolute ``.onnx``
path (used verbatim — the user's own model). The RUNTIME is the ``piper-tts`` package, which
wraps **native** ``onnxruntime`` — a compiled, platform-specific wheel that cannot be vendored
cross-platform and so is not shipped either.

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

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.registry import register_tts
from omnia.core.providers.tts.voice_models import (
    DownloadFeedback,
    PiperVoiceStore,
    default_voice_store,
)
from omnia.core.runtime.native_runtime import (
    NativeRuntimeManager,
    NativeRuntimeSpec,
    default_manager,
    register_native_runtime,
)

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

_logger = get_logger("piper")

# The default Vietnamese voice; its weights are fetched on first use, not packaged.
_DEFAULT_VOICE = "vi_VN-vais1000-medium"


def _require_model_file(model_path: str) -> None:
    """Raise a legible error when the resolved model file is not on disk.

    Only reachable for a voice the user pointed at by PATH: a catalog voice NAME is resolved —
    and downloaded if missing — by the voice store before a runner ever sees it. Shared by both
    runners so the advice cannot drift between the two paths.

    Raises:
        ProviderError: If ``model_path`` is not an existing file.
    """
    if not os.path.isfile(model_path):
        raise ProviderError(
            f"piper voice model not found at {model_path}. Pick one of Omnia's piper voices in "
            "the voice picker (it downloads itself the first time you use it), or point the "
            "voice at an existing .onnx file whose .onnx.json sits next to it."
        )


class AnkiProgressFeedback(DownloadFeedback):
    """Wires a voice download to the progress dialog synthesis is already running under.

    The interactive paths (the editor button, the Studio preview, the account voice test) run
    synthesis off the Qt main thread inside a ``QueryOp`` **with** an indeterminate progress
    dialog: its label is the one channel a 60 MB download has to say how far it has got, and
    its Cancel button the one way out of a multi-minute fetch on a slow link. The review-time
    pre-generation path deliberately runs without a dialog (it must not interrupt a review), so
    there both directions are no-ops — as they are outside Anki, in tests and headless imports.
    Neither is a failure worth aborting a working download for, so both are swallowed here.
    """

    def report(self, message: str) -> None:
        # Boundary: a cosmetic label must never break an otherwise working download.
        try:
            anki_compat.progress_label(message)
        except Exception:
            _logger.debug("piper: could not update the progress dialog", exc_info=True)

    def cancelled(self) -> bool:
        try:
            return anki_compat.progress_was_cancelled()
        except Exception:  # no dialog / no Anki: nobody can have clicked Cancel
            _logger.debug("piper: could not read the cancel flag", exc_info=True)
            return False


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

    def ensure_ready(self) -> None:  # noqa: B027
        """Raise if this runner could not synthesize at all, BEFORE a model is resolved.

        Resolving a model may cost a 60 MB download; deciding whether piper can run at all is a
        directory stat or an ``import``. Doing the cheap, always-failing check first is what
        stops a user who never enabled the opt-in native runtime from paying for a fetch whose
        synthesis cannot succeed (ADR-015).

        The empty body is a concrete default, not a forgotten ``@abstractmethod`` (hence the
        B027 waiver): a runner that is always ready — an in-test fake — inherits it and says
        nothing, which is exactly right.

        Raises:
            ProviderError: If the runtime this runner needs is not available.
        """

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

    def ensure_ready(self) -> None:
        """Check the managed venv is installed — one ``is_dir``, no network, no side effects."""
        (self._manager or default_manager()).require_installed(SPEC)

    def run(self, text: str, model_path: str) -> bytes:
        _require_model_file(model_path)
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

    @staticmethod
    def _piper_voice_class() -> Any:
        """Import ``piper.PiperVoice`` or raise the one "how to get it" message.

        Shared by :meth:`ensure_ready` and :meth:`run` so the advice cannot drift between the
        cheap pre-check and the real call; the second import is a ``sys.modules`` hit.

        Raises:
            ProviderError: If the ``piper-tts`` package is not importable.
        """
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise ProviderError(
                "piper requires the 'piper-tts' package (it wraps native onnxruntime, so it "
                "can't ship with the add-on). Run `pip install piper-tts`, or pick edge_tts / "
                "google_translate (free, nothing to install)."
            ) from exc
        return PiperVoice

    def ensure_ready(self) -> None:
        """Check ``piper-tts`` is importable — before a voice is resolved or downloaded."""
        self._piper_voice_class()

    def run(self, text: str, model_path: str) -> bytes:
        import io
        import wave

        voice_class = self._piper_voice_class()
        _require_model_file(model_path)
        # Boundary: surface any piper/onnx failure as a ProviderError.
        try:
            voice = voice_class.load(model_path)
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
    # The voices Omnia offers. Their weights are not packaged — the store fetches them on first
    # use (see voice_models.DOWNLOADABLE_VOICES). A field's voice is the NAME here; another
    # voice can be dropped into the voices dir and typed in the picker.
    CURATED_VOICES: ClassVar[list[TTSVoice]] = [
        TTSVoice("piper", _DEFAULT_VOICE, "Vietnamese", "vais1000", "Female", "", "vi"),
    ]

    def __init__(
        self,
        model: str = "",
        runner: Optional[PiperRunner] = None,
        store: Optional[PiperVoiceStore] = None,
    ) -> None:
        self._model = model
        self._runner = runner or SidecarPiperRunner()
        # Inject for tests; default lazily — building the store resolves Anki's user_files, so
        # constructing a provider must not do it (construction stays I/O-free).
        self._store = store

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> PiperTTS:
        return cls(model=config.get("model", ""))

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        """Synthesize ``text`` to WAV, checking the runtime BEFORE resolving the voice.

        The order is the point (ADR-015). ``ensure_ready`` is a directory stat; resolving the
        voice may be a 60 MB download. Piper's native runtime is opt-in and OFF by default, so
        the reverse order makes a user who never enabled it wait out an entire fetch to be told
        the synthesis could never have run.
        """
        self._runner.ensure_ready()
        return self._runner.run(text, self._resolve_model_path(voice))

    def _voice_store(self) -> PiperVoiceStore:
        """The injected store, or the process-wide one (built on first use)."""
        if self._store is None:
            self._store = default_voice_store()
        return self._store

    def _resolve_model_path(self, voice: Optional[str]) -> str:
        """Resolve a voice NAME or path to an ``.onnx`` model file on disk.

        A voice NAME (e.g. ``"vi_VN-vais1000-medium"``) goes through the voice store's ladder:
        the ``user_files`` copy, else the copy beside the add-on, else a one-time download that
        reports progress into — and takes its Cancel from — Anki's progress dialog. An ABSOLUTE
        ``.onnx`` path is the user's own model and is used verbatim; a RELATIVE ``.onnx`` path
        names a file they dropped into a voices dir, which is looked up there and never
        downloaded. Falls back to the default voice.

        Only ever called after :meth:`PiperRunner.ensure_ready` has passed, so nothing here can
        download a voice for a runtime that is not installed.
        """
        name = (voice or self._model or _DEFAULT_VOICE).strip()
        if not name:
            raise ProviderError(
                "piper needs a voice — a voice name or a path to a .onnx file"
            )
        path = Path(name)
        store = self._voice_store()
        if path.suffix == ".onnx":
            if path.is_absolute():
                return str(path)
            # A bare "<voice>.onnx" is a VOICE, not a file. That spelling is what
            # providers.example.toml suggests and what every setup made before the weights
            # stopped being packaged already holds, and it used to resolve because the file
            # was always there. Reading it as a path now would break exactly the configs that
            # worked yesterday — including the catalog's own default voice. A name with a
            # directory in it is still a path: that one can only have been meant as one.
            if path.parent == Path(".") and store.knows(path.stem):
                return str(store.resolve(path.stem, feedback=AnkiProgressFeedback()))
            return str(store.local_path(path))
        return str(store.resolve(name, feedback=AnkiProgressFeedback()))
