"""16-bit PCM WAV toolkit built on nothing but ``wave``/``struct``/``math``.

Audio cloze (silence or a beep where the answer word is spoken) needs to cut and splice PCM.
Two constraints shape this module:

* **No dependency may be added for it.** Vendored deps must be pure-Python, and every audio
  library worth the name ships compiled wheels, so the stdlib is the whole toolbox.
* **``audioop`` is REMOVED in Python 3.13** (PEP 594), the interpreter the latest Anki bundles.
  Sample math is therefore done here with :mod:`struct` unpack/repack, never ``audioop``.

Scope is deliberately narrow: uncompressed 16-bit PCM only — what piper and viet-tts emit.
Anything else (MP3 providers, 8-bit, µ-law) is rejected loudly by :class:`WavFormatError` so
a caller can degrade honestly instead of splicing garbage into a card.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import Sequence
from dataclasses import dataclass

#: The only sample width this toolkit reads or writes, in bytes.
SAMPLE_WIDTH = 2
_MAX_AMPLITUDE = 32767
_MIN_AMPLITUDE = -32768
_MS_PER_SECOND = 1000


class WavFormatError(ValueError):
    """Raised when audio is not the uncompressed 16-bit PCM this toolkit can handle."""


def _frame_count_for(ms: float, framerate: int) -> int:
    """Return how many frames ``ms`` milliseconds occupy at ``framerate``."""
    if ms < 0:
        raise ValueError(f"duration must be >= 0 ms, got {ms}")
    return round(ms * framerate / _MS_PER_SECOND)


def _pack(samples: Sequence[int]) -> bytes:
    """Pack signed 16-bit ``samples`` into little-endian PCM frames."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _clamp(value: int) -> int:
    """Clip ``value`` into the signed 16-bit range so packing can never overflow."""
    return max(_MIN_AMPLITUDE, min(_MAX_AMPLITUDE, value))


@dataclass(frozen=True)
class WavClip:
    """One uncompressed 16-bit PCM clip: its stream parameters plus its raw frames.

    Immutable on purpose — every operation (:meth:`concat`, :meth:`fade_edges`) returns a new
    clip, so a splice can never corrupt the source audio a caller still holds.

    Attributes:
        nchannels: Channel count (1 = mono).
        sampwidth: Bytes per sample; must be :data:`SAMPLE_WIDTH`.
        framerate: Samples per second per channel (e.g. ``22050``).
        frames: Interleaved little-endian PCM frames.
    """

    nchannels: int
    sampwidth: int
    framerate: int
    frames: bytes

    def __post_init__(self) -> None:
        """Validate the parameters so an invalid clip can never exist.

        Raises:
            WavFormatError: If the parameters are outside this toolkit's 16-bit PCM scope, or
                ``frames`` does not hold a whole number of frames.
        """
        if self.sampwidth != SAMPLE_WIDTH:
            raise WavFormatError(
                f"only {SAMPLE_WIDTH * 8}-bit PCM is supported, got "
                f"{self.sampwidth * 8}-bit audio."
            )
        if self.nchannels < 1:
            raise WavFormatError(f"channel count must be >= 1, got {self.nchannels}.")
        if self.framerate < 1:
            raise WavFormatError(f"frame rate must be >= 1, got {self.framerate}.")
        if len(self.frames) % self.frame_size:
            raise WavFormatError(
                f"{len(self.frames)} bytes is not a whole number of "
                f"{self.frame_size}-byte frames."
            )

    # --- shape -----------------------------------------------------------------------

    @property
    def params(self) -> tuple[int, int, int]:
        """The ``(nchannels, sampwidth, framerate)`` triple clips must share to be spliced."""
        return (self.nchannels, self.sampwidth, self.framerate)

    @property
    def frame_size(self) -> int:
        """Bytes per frame (one sample per channel)."""
        return self.nchannels * self.sampwidth

    @property
    def frame_count(self) -> int:
        """Number of frames in the clip."""
        return len(self.frames) // self.frame_size

    @property
    def duration_ms(self) -> float:
        """Playing time of the clip in milliseconds."""
        return self.frame_count * _MS_PER_SECOND / self.framerate

    def samples(self) -> tuple[int, ...]:
        """Return every sample as a signed int, channels interleaved."""
        return struct.unpack(f"<{len(self.frames) // self.sampwidth}h", self.frames)

    # --- construction ----------------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes) -> WavClip:
        """Parse a WAV file and verify it is uncompressed 16-bit PCM.

        Args:
            data: The complete bytes of a ``.wav`` file.

        Returns:
            The parsed clip.

        Raises:
            WavFormatError: If ``data`` is not a readable WAV stream, is compressed, or does
                not use 16-bit samples.
        """
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                nchannels = reader.getnchannels()
                sampwidth = reader.getsampwidth()
                framerate = reader.getframerate()
                comptype = reader.getcomptype()
                frames = reader.readframes(reader.getnframes())
        except (wave.Error, EOFError) as exc:
            raise WavFormatError(f"not a readable WAV stream: {exc}") from exc
        if comptype != "NONE":
            raise WavFormatError(
                f"compressed WAV (comptype={comptype!r}); only uncompressed PCM is supported."
            )
        if sampwidth != SAMPLE_WIDTH:
            raise WavFormatError(
                f"only {SAMPLE_WIDTH * 8}-bit PCM is supported, got {sampwidth * 8}-bit audio."
            )
        return cls(nchannels, sampwidth, framerate, frames)

    @classmethod
    def silence(cls, ms: float, *, like: WavClip) -> WavClip:
        """Return ``ms`` milliseconds of silence with ``like``'s stream parameters.

        Args:
            ms: Duration in milliseconds.
            like: The clip whose parameters (and therefore splice compatibility) to copy.

        Returns:
            A silent clip.
        """
        count = _frame_count_for(ms, like.framerate)
        return cls(
            like.nchannels,
            like.sampwidth,
            like.framerate,
            b"\x00" * (count * like.frame_size),
        )

    @classmethod
    def sine_beep(
        cls, ms: float, hz: float, *, like: WavClip, gain_db: float = -3.0
    ) -> WavClip:
        """Return a sine tone of ``ms`` milliseconds at ``hz``, with ``like``'s parameters.

        The tone is attenuated by ``gain_db`` because a full-scale beep spliced into speech is
        painfully loud on headphones; the resulting peak is exactly
        ``32767 * 10 ** (gain_db / 20)``.

        Args:
            ms: Duration in milliseconds.
            hz: Tone frequency.
            like: The clip whose parameters to copy.
            gain_db: Attenuation in decibels relative to full scale (negative = quieter).

        Returns:
            A beep clip. Splice it through :meth:`fade_edges` to avoid click artefacts.
        """
        count = _frame_count_for(ms, like.framerate)
        amplitude = int(_MAX_AMPLITUDE * 10 ** (gain_db / 20.0))
        step = 2 * math.pi * hz / like.framerate
        values: list[int] = []
        for frame in range(count):
            sample = _clamp(int(amplitude * math.sin(step * frame)))
            values.extend([sample] * like.nchannels)
        return cls(like.nchannels, like.sampwidth, like.framerate, _pack(values))

    @classmethod
    def concat(cls, clips: Sequence[WavClip]) -> WavClip:
        """Join ``clips`` end to end.

        Args:
            clips: The clips to join, in order. Must all share the same parameters — resampling
                or channel mixing is out of scope, and joining mismatched audio silently would
                produce chipmunk speech.

        Returns:
            One clip holding every input's frames.

        Raises:
            WavFormatError: If ``clips`` is empty (there would be no parameters to give the
                result) or the clips disagree on channels, sample width or frame rate.
        """
        items = list(clips)
        if not items:
            raise WavFormatError("cannot concatenate an empty sequence of clips.")
        head = items[0]
        for index, clip in enumerate(items[1:], start=1):
            if clip.params != head.params:
                raise WavFormatError(
                    f"clip {index} has parameters {clip.params}, expected {head.params}."
                )
        return cls(
            head.nchannels,
            head.sampwidth,
            head.framerate,
            b"".join(clip.frames for clip in items),
        )

    # --- transforms ------------------------------------------------------------------

    def fade_edges(self, ms: float = 10.0) -> WavClip:
        """Return a copy with a linear fade in at the start and out at the end.

        Cutting speech at an arbitrary sample leaves a step in the waveform, which plays back
        as a click. Ramping the first and last few milliseconds to zero removes it, which is
        what makes a spliced clip sound like one recording.

        Args:
            ms: Length of each ramp. Capped at half the clip so the two ramps never overlap.

        Returns:
            A new clip; ``self`` when the clip is too short to carry a ramp.
        """
        ramp = min(_frame_count_for(ms, self.framerate), self.frame_count // 2)
        if ramp <= 0:
            return self
        values = list(self.samples())
        last = self.frame_count - 1
        for offset in range(ramp):
            gain = offset / ramp
            for channel in range(self.nchannels):
                head = offset * self.nchannels + channel
                tail = (last - offset) * self.nchannels + channel
                values[head] = int(values[head] * gain)
                values[tail] = int(values[tail] * gain)
        return WavClip(self.nchannels, self.sampwidth, self.framerate, _pack(values))

    # --- serialisation ---------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Return the clip as the bytes of a complete ``.wav`` file."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(self.nchannels)
            writer.setsampwidth(self.sampwidth)
            writer.setframerate(self.framerate)
            writer.writeframes(self.frames)
        return buffer.getvalue()
