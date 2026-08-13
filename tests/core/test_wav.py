"""Tests for the stdlib 16-bit PCM toolkit (``core/audio/wav.py``).

The frame math is checked at 22050 Hz mono because that is what the bundled piper voice emits
— the audio-cloze splice happens at exactly these parameters.
"""

from __future__ import annotations

import struct

import pytest

from omnia.core.audio.wav import SAMPLE_WIDTH, WavClip, WavFormatError

_RATE = 22050


def _clip(samples: list[int], *, framerate: int = _RATE, nchannels: int = 1) -> WavClip:
    """Build a clip from raw sample values (channels interleaved)."""
    return WavClip(
        nchannels, SAMPLE_WIDTH, framerate, struct.pack(f"<{len(samples)}h", *samples)
    )


def _wav_file(
    samples: list[int],
    *,
    framerate: int = _RATE,
    nchannels: int = 1,
    sampwidth: int = 2,
) -> bytes:
    """Hand-roll a canonical 44-byte-header WAV file (so parsing is tested, not round-tripped)."""
    data = struct.pack(f"<{len(samples)}h", *samples)
    return _wav_header(len(data), framerate, nchannels, sampwidth, fmt=1) + data


def _wav_header(
    data_size: int, framerate: int, nchannels: int, sampwidth: int, fmt: int
) -> bytes:
    """Return the canonical 44-byte RIFF/WAVE header for a ``data`` chunk of ``data_size``."""
    block_align = nchannels * sampwidth
    return (
        b"RIFF"
        + struct.pack("<L", 36 + data_size)
        + b"WAVEfmt "
        + struct.pack("<L", 16)
        + struct.pack(
            "<HHLLHH",
            fmt,
            nchannels,
            framerate,
            framerate * block_align,
            block_align,
            sampwidth * 8,
        )
        + b"data"
        + struct.pack("<L", data_size)
    )


class TestValidation:
    def test_rejects_a_sample_width_other_than_16_bit(self):
        with pytest.raises(WavFormatError, match="16-bit"):
            WavClip(1, 1, _RATE, b"\x00\x00")

    def test_rejects_frames_that_are_not_whole(self):
        with pytest.raises(WavFormatError, match="whole number"):
            WavClip(1, SAMPLE_WIDTH, _RATE, b"\x00")

    def test_rejects_impossible_channel_and_frame_rate(self):
        with pytest.raises(WavFormatError, match="channel count"):
            WavClip(0, SAMPLE_WIDTH, _RATE, b"")
        with pytest.raises(WavFormatError, match="frame rate"):
            WavClip(1, SAMPLE_WIDTH, 0, b"")


class TestFrameMath:
    def test_duration_of_a_second_of_mono_audio(self):
        clip = _clip([0] * _RATE)
        assert clip.frame_count == _RATE
        assert clip.duration_ms == pytest.approx(1000.0)

    def test_stereo_frames_count_channel_pairs(self):
        clip = _clip([1, 2, 3, 4], nchannels=2)
        assert clip.frame_size == 4
        assert clip.frame_count == 2
        assert clip.samples() == (1, 2, 3, 4)

    def test_silence_length_follows_the_frame_rate(self):
        clip = _clip([0])
        assert WavClip.silence(1000, like=clip).frame_count == _RATE
        assert WavClip.silence(10, like=clip).frame_count == 220  # 10 ms @ 22050 Hz
        assert WavClip.silence(0, like=clip).frames == b""

    def test_silence_round_trips_a_measured_duration(self):
        # The audio-cloze invariant: a spoken word is replaced by EXACTLY as many frames.
        word = _clip([7] * 1234)
        assert WavClip.silence(word.duration_ms, like=word).frame_count == 1234

    def test_silence_is_actually_silent_and_matches_the_source_params(self):
        clip = _clip([1, 2], nchannels=2, framerate=16000)
        quiet = WavClip.silence(5, like=clip)
        assert quiet.params == clip.params
        assert set(quiet.samples()) == {0}

    def test_negative_duration_is_rejected(self):
        with pytest.raises(ValueError, match="duration"):
            WavClip.silence(-1, like=_clip([0]))


class TestParsing:
    def test_round_trips_through_bytes(self):
        clip = _clip([0, 1000, -1000, 32767, -32768])
        parsed = WavClip.from_bytes(clip.to_bytes())
        assert parsed == clip

    def test_parses_a_hand_rolled_file(self):
        parsed = WavClip.from_bytes(_wav_file([5, -5]))
        assert parsed.params == (1, SAMPLE_WIDTH, _RATE)
        assert parsed.samples() == (5, -5)

    def test_rejects_8_bit_pcm(self):
        raw = _wav_header(2, _RATE, 1, 1, fmt=1) + b"\x80\x80"
        with pytest.raises(WavFormatError, match="8-bit"):
            WavClip.from_bytes(raw)

    def test_rejects_a_compressed_stream(self):
        # Format tag 6 is A-law: a codec the stdlib cannot decode on 3.13 (audioop is gone).
        raw = _wav_header(2, _RATE, 1, 1, fmt=6) + b"\x00\x00"
        with pytest.raises(WavFormatError):
            WavClip.from_bytes(raw)

    def test_rejects_a_non_wav_payload(self):
        with pytest.raises(WavFormatError, match="not a readable WAV stream"):
            WavClip.from_bytes(b"ID3\x04\x00not an mp3 either")


class TestConcat:
    def test_joins_frames_in_order(self):
        joined = WavClip.concat([_clip([1, 2]), _clip([3]), _clip([4, 5])])
        assert joined.samples() == (1, 2, 3, 4, 5)
        assert joined.params == (1, SAMPLE_WIDTH, _RATE)

    def test_golden_bytes_of_a_spliced_file(self):
        joined = WavClip.concat([_clip([1, -1]), _clip([2, -2])])
        payload = struct.pack("<4h", 1, -1, 2, -2)
        assert (
            joined.to_bytes()
            == _wav_header(len(payload), _RATE, 1, SAMPLE_WIDTH, fmt=1) + payload
        )

    def test_rejects_a_frame_rate_mismatch(self):
        with pytest.raises(WavFormatError, match="clip 1"):
            WavClip.concat([_clip([1]), _clip([1], framerate=16000)])

    def test_rejects_a_channel_mismatch(self):
        with pytest.raises(WavFormatError, match="expected"):
            WavClip.concat([_clip([1, 2]), _clip([1, 2], nchannels=2)])

    def test_rejects_an_empty_sequence(self):
        with pytest.raises(WavFormatError, match="empty"):
            WavClip.concat([])


class TestSineBeep:
    def test_peak_is_bounded_by_the_gain(self):
        beep = WavClip.sine_beep(50, 880, like=_clip([0]), gain_db=-3.0)
        ceiling = int(32767 * 10 ** (-3.0 / 20.0))
        assert max(abs(s) for s in beep.samples()) <= ceiling

    def test_quieter_gain_lowers_the_peak(self):
        reference = _clip([0])
        loud = WavClip.sine_beep(50, 880, like=reference, gain_db=-3.0)
        soft = WavClip.sine_beep(50, 880, like=reference, gain_db=-20.0)
        assert max(abs(s) for s in soft.samples()) < max(abs(s) for s in loud.samples())

    def test_length_and_params_follow_the_reference_clip(self):
        beep = WavClip.sine_beep(
            100, 440, like=_clip([0, 0], nchannels=2), gain_db=-6.0
        )
        assert beep.params == (2, SAMPLE_WIDTH, _RATE)
        assert beep.frame_count == 2205  # 100 ms @ 22050 Hz

    def test_channels_carry_the_same_waveform(self):
        beep = WavClip.sine_beep(5, 440, like=_clip([0, 0], nchannels=2))
        samples = beep.samples()
        assert samples[0::2] == samples[1::2]

    def test_starts_at_silence_and_oscillates(self):
        beep = WavClip.sine_beep(50, 440, like=_clip([0]))
        samples = beep.samples()
        assert samples[0] == 0
        assert max(samples) > 0 > min(samples)


class TestFadeEdges:
    def test_ramps_are_monotonic_on_a_constant_signal(self):
        clip = _clip([10000] * 2205)  # 100 ms
        faded = clip.fade_edges(10).samples()
        ramp = 220  # 10 ms @ 22050 Hz
        assert faded[:ramp] == tuple(sorted(faded[:ramp]))
        assert faded[-ramp:] == tuple(sorted(faded[-ramp:], reverse=True))

    def test_edges_reach_silence_and_the_middle_is_untouched(self):
        clip = _clip([10000] * 2205)
        faded = clip.fade_edges(10)
        samples = faded.samples()
        assert samples[0] == 0
        assert samples[-1] == 0
        assert set(samples[220:-220]) == {10000}

    def test_length_and_params_are_preserved(self):
        clip = _clip([10000, 10000] * 2205, nchannels=2)
        faded = clip.fade_edges(10)
        assert faded.params == clip.params
        assert faded.frame_count == clip.frame_count

    def test_every_channel_is_faded(self):
        clip = _clip([10000, 10000] * 100, nchannels=2)
        samples = clip.fade_edges(1).samples()
        assert samples[0] == 0
        assert samples[1] == 0

    def test_ramp_is_capped_at_half_the_clip(self):
        clip = _clip([10000] * 100)
        faded = clip.fade_edges(1000).samples()
        # 50-frame ramps meeting in the middle: the loudest sample is the last of the fade-in.
        assert faded[49] == max(faded)
        assert faded[0] == 0
        assert faded[-1] == 0

    def test_a_clip_too_short_to_ramp_is_returned_unchanged(self):
        clip = _clip([10000])
        assert clip.fade_edges(10) is clip
        assert clip.fade_edges(0) is clip
