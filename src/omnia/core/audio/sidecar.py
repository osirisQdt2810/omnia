"""The ``audio`` native runtime: a codec for the formats the stdlib cannot open (ADR-005).

:mod:`omnia.core.audio.wav` can cut and splice 16-bit PCM and nothing else, which covers the
WAV providers (piper, viet-tts) and no one else. Every cloud voice — edge_tts, google_translate,
google_cloud, the OpenAI family — returns **MP3**, and there is no pure-Python MP3 decoder worth
shipping, so audio surgery on those voices needs a real codec.

A codec is exactly the kind of dependency ADR-005 exists for: ``av`` (PyAV) bundles FFmpeg as a
compiled, per-platform wheel, so it can neither be vendored (ADR-004) nor installed into Anki's
frozen interpreter. It therefore lives in an add-on-managed venv and is driven out of process by
:mod:`omnia.core.audio.sidecar_cli`.

Two rules this seam keeps:

* **It never auto-installs.** Like piper and viet-tts, the runtime is an explicit toggle in
  Smart Notes → Options → Advanced; a caller that needs it and does not have it gets an
  actionable :class:`~omnia.core.providers.errors.ProviderError` naming that screen.
* **It only crosses the codec boundary.** Decode in, encode out — the splice stays in
  :mod:`omnia.core.audio.wav`, where the test suite can check it byte for byte.

Pure and headless: no ``aqt``/``anki`` import, and the manager is injectable so tests script
the subprocess instead of running one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.runtime.native_runtime import (
    NativeRuntimeManager,
    NativeRuntimeSpec,
    default_manager,
    register_native_runtime,
)

#: The transcoder script the venv's python runs. A sibling module rather than a console script
#: (piper's ``{bin}/piper``) because ``av`` is a library: we ship the CLI ourselves.
_CLI_SCRIPT = Path(__file__).with_name("sidecar_cli.py")

#: The managed-venv spec (ADR-005). Its own ``"audio"`` section, so the Advanced tab lists it
#: apart from the TTS engines — it is a codec every voice can borrow, not a voice.
SPEC: NativeRuntimeSpec = register_native_runtime(
    NativeRuntimeSpec(
        name="audio",
        section="audio",
        label="Audio codec (MP3 voices, PyAV)",
        pip_packages=("av",),
        # PyAV's wheels bundle FFmpeg, so the download is chunky but a one-off, and the newest
        # wheels no longer build for 3.9 — the same floor viet-tts needs.
        min_python=(3, 10),
        mode="cli",
        size_hint="~40 MB",
        cli_argv=("{python}", str(_CLI_SCRIPT)),
    )
)


class AudioSidecar:
    """Decodes and encodes compressed audio by running PyAV in the managed venv.

    Stateless apart from the injected manager, so one instance is safe to share across the
    generation worker threads.
    """

    def __init__(self, manager: Optional[NativeRuntimeManager] = None) -> None:
        """Initialise the sidecar.

        Args:
            manager: The runtime manager to drive. Defaults to the process-wide one, resolved
                lazily so importing this module never touches Anki's paths.
        """
        self._manager = manager

    @property
    def manager(self) -> NativeRuntimeManager:
        """The runtime manager this sidecar drives (the process-wide one by default)."""
        return self._manager or default_manager()

    def is_installed(self) -> bool:
        """Whether the ``audio`` venv is present, so MP3 voices can be spliced right now.

        Never raises: this is read while painting the tools picker, and a manager that cannot
        reach its envs directory means "not installed", not "crash the dialog".
        """
        try:
            return self.manager.is_installed(SPEC)
        except OSError:
            return False

    def decode(self, data: bytes) -> bytes:
        """Return ``data`` (any format FFmpeg reads) as the bytes of a 16-bit PCM WAV file.

        Args:
            data: The provider's audio bytes (MP3, OGG, …).

        Returns:
            A complete ``.wav`` file :class:`~omnia.core.audio.wav.WavClip` can parse.

        Raises:
            ProviderError: If the runtime is not installed (with the "enable it in Advanced"
                message), or the transcode failed.
        """
        return self._transcode("decode", data)

    def encode(self, wav: bytes) -> bytes:
        """Return the 16-bit PCM WAV ``wav`` re-encoded as MP3.

        Handing a spliced sentence back as WAV would work, but a minute of 22 kHz mono PCM is
        ~2.5 MB against ~230 KB of MP3, and this audio is written into the collection's media
        folder and synced.

        Args:
            wav: The bytes of a ``.wav`` file.

        Returns:
            The MP3 bytes.

        Raises:
            ProviderError: If the runtime is not installed, or the transcode failed.
        """
        return self._transcode("encode", wav)

    def _transcode(self, command: str, data: bytes) -> bytes:
        """Run one ``sidecar_cli`` command over ``data`` and return what it wrote.

        Both payloads travel through temp FILES, not the child's stdin/stdout: Windows opens
        the standard streams in text mode, which would corrupt any byte sequence containing a
        carriage return (the same reason the piper runner writes to ``-f <path>``).
        """
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / f"{command}-in"
            target = Path(tmp) / f"{command}-out"
            source.write_bytes(data)
            code = self.manager.run_in_venv(SPEC, [command, str(source), str(target)])
            if code != 0 or not target.exists():
                raise ProviderError(
                    f"the audio runtime could not {command} this clip (exit={code}). "
                    "Re-install it in Smart Notes → Options → Advanced if this persists."
                )
            return target.read_bytes()
