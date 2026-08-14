"""Audio transcoder executed INSIDE the add-on-managed ``av`` sidecar venv (ADR-005).

This file is shipped with the add-on but is **never imported by Anki's interpreter**: it
imports :mod:`av` (PyAV), a compiled wheel that cannot be vendored and must not be installed
into Anki's frozen Python. :class:`~omnia.core.audio.sidecar.AudioSidecar` runs it as a
one-shot subprocess in the venv the :class:`~omnia.core.native_runtime.NativeRuntimeManager`
created, so the native FFmpeg ABI belongs to that venv's own interpreter.

Two commands, both file-in/file-out::

    python sidecar_cli.py decode <input> <output.wav>   # anything -> 16-bit PCM WAV
    python sidecar_cli.py encode <input.wav> <output>   # 16-bit PCM WAV -> MP3

Files rather than stdin/stdout deliberately: on Windows the standard streams are opened in
text mode, so a raw ``\\r`` in an audio payload would be rewritten in transit. The piper
runner already passes audio through a temp file for the same reason.

The SPLICE is not done here. It lives in :mod:`omnia.core.audio.wav`, which the test suite
exercises byte for byte — this process only ever crosses the codec boundary, so the one part
that cannot run in CI stays as small as it can be.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from typing import Any

#: Sample rates libmp3lame accepts. Encoding at anything else fails inside FFmpeg, so an input
#: recorded at an unsupported rate is resampled to the nearest supported one.
_MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)

_SAMPLE_WIDTH = 2  # 16-bit PCM: the only format core/audio/wav.py reads or writes.


def _mono_or_stereo(channels: int) -> str:
    """Return the FFmpeg channel layout name for ``channels`` (anything >1 is downmixed)."""
    return "mono" if channels <= 1 else "stereo"


def _plane_bytes(frame: Any, channels: int) -> bytes:
    """Return one packed ``s16`` frame's samples, trimmed of the plane's padding.

    PyAV allocates each plane in aligned blocks, so ``bytes(plane)`` can be longer than the
    audio it holds; the valid part is ``samples * channels * 2`` bytes for a packed format.
    Keeping the tail would splice audible garbage between frames.
    """
    return bytes(frame.planes[0])[: frame.samples * channels * _SAMPLE_WIDTH]


def _decode(source: Path, target: Path) -> None:
    """Decode ``source`` (any container FFmpeg reads) into a 16-bit PCM WAV at ``target``."""
    import av

    chunks: list[bytes] = []
    with av.open(str(source)) as container:
        stream = container.streams.audio[0]
        rate = int(stream.rate or 44100)
        channels = 1 if (stream.channels or 1) <= 1 else 2
        resampler = av.AudioResampler(
            format="s16", layout=_mono_or_stereo(channels), rate=rate
        )
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(_plane_bytes(resampled, channels))
        for resampled in resampler.resample(None):  # flush the resampler's tail
            chunks.append(_plane_bytes(resampled, channels))

    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(_SAMPLE_WIDTH)
        writer.setframerate(rate)
        writer.writeframes(b"".join(chunks))


def _encode(source: Path, target: Path) -> None:
    """Encode the WAV at ``source`` into an MP3 at ``target``.

    The rate is carried over when libmp3lame supports it (piper's 22050 Hz and viet-tts's
    24000 Hz both do); anything else resamples to 44100 rather than failing the whole field.
    """
    import av

    with av.open(str(source)) as container:
        stream = container.streams.audio[0]
        rate = int(stream.rate or 44100)
        rate = rate if rate in _MP3_RATES else 44100
        channels = 1 if (stream.channels or 1) <= 1 else 2
        layout = _mono_or_stereo(channels)
        with av.open(str(target), "w", format="mp3") as output:
            # The layout must be declared on the STREAM, not only on the resampler: without it
            # libmp3lame defaults to stereo and duplicates a mono voice into two channels,
            # doubling the size of every clip this re-encode exists to keep small.
            out_stream = output.add_stream("mp3", rate=rate, layout=layout)
            resampler = av.AudioResampler(
                format=out_stream.format.name,
                layout=layout,
                rate=rate,
            )
            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    output.mux(out_stream.encode(resampled))
            for resampled in resampler.resample(None):
                output.mux(out_stream.encode(resampled))
            output.mux(out_stream.encode(None))  # flush the encoder


_COMMANDS = {"decode": _decode, "encode": _encode}


def main(argv: list[str]) -> int:
    """Run one transcode. Returns the process exit code (0 = the output file was written)."""
    if len(argv) != 3 or argv[0] not in _COMMANDS:
        sys.stderr.write("usage: sidecar_cli.py decode|encode <input> <output>\n")
        return 2
    try:
        _COMMANDS[argv[0]](Path(argv[1]), Path(argv[2]))
    except Exception as exc:  # boundary: the parent only sees the exit code + this line
        sys.stderr.write(f"{argv[0]} failed: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
