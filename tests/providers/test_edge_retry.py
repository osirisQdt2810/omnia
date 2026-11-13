"""Edge TTS retries a transient transport failure — and only a transient one.

Under a bulk run Edge sometimes times out a session that succeeds on a second try. The
transport is faked at the session level (one scripted outcome per Edge session), and the
retry policy gets a recording ``sleep`` so nothing here actually waits.
"""

from __future__ import annotations

import ssl

import pytest

from omnia.core.network.http import RetryPolicy
from omnia.core.network.websocket import (
    WebSocketClient,
    WebSocketClosedError,
    WebSocketError,
)
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.edge_tts import EdgeProtocolSynthesizer


class _ScriptedSynthesizer(EdgeProtocolSynthesizer):
    """Each Edge session returns (or raises) the next scripted outcome."""

    def __init__(self, outcomes: list[object]) -> None:
        self.sleeps: list[float] = []
        super().__init__(
            retry=RetryPolicy(sleep=self.sleeps.append, jitter=lambda: 0.0)
        )
        self._outcomes = list(outcomes)
        self.sessions: list[str] = []

    def _synthesize_one_session(self, text: str, voice: str, rate: str = "+0%"):
        self.sessions.append(text)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _upgrade_refused(status: int) -> WebSocketError:
    return WebSocketError(f"WebSocket upgrade failed: 'HTTP/1.1 {status} x'", status)


class TestTransientFailuresAreRetried:
    def test_a_timeout_then_success_returns_the_audio(self):
        synth = _ScriptedSynthesizer(
            [TimeoutError("The read operation timed out"), b"MP3"]
        )

        assert synth.synthesize("The phone remained intact.", "v") == b"MP3"
        assert len(synth.sessions) == 2
        assert synth.sleeps == [0.5]

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionResetError("reset by peer"),
            WebSocketClosedError("connection closed mid-frame"),
            _upgrade_refused(503),
            _upgrade_refused(429),
        ],
    )
    def test_each_transient_kind_is_retried(self, error):
        synth = _ScriptedSynthesizer([error, b"MP3"])

        assert synth.synthesize("hello", "v") == b"MP3"
        assert len(synth.sessions) == 2

    def test_a_persistent_timeout_fails_after_the_bounded_attempts(self):
        synth = _ScriptedSynthesizer([TimeoutError("timed out")] * 5)

        with pytest.raises(ProviderError, match="timed out"):
            synth.synthesize("hello", "v")
        assert len(synth.sessions) == 3
        assert synth.sleeps == [0.5, 1.0]

    def test_a_retried_chunk_does_not_duplicate_earlier_chunks(self, monkeypatch):
        monkeypatch.setattr(EdgeProtocolSynthesizer, "_MAX_CHUNK_BYTES", 5)
        synth = _ScriptedSynthesizer([b"A", TimeoutError("timed out"), b"B"])

        assert synth.synthesize("aaaa bbbb", "v") == b"AB"
        assert synth.sessions == ["aaaa", "bbbb", "bbbb"]


class TestRealFailuresAreNotRetried:
    def test_a_refused_token_raises_at_once(self):
        synth = _ScriptedSynthesizer([_upgrade_refused(403), b"MP3"])

        with pytest.raises(ProviderError, match="403"):
            synth.synthesize("hello", "v")
        assert len(synth.sessions) == 1
        assert synth.sleeps == []

    def test_a_bad_certificate_raises_at_once(self):
        synth = _ScriptedSynthesizer([ssl.SSLCertVerificationError("bad"), b"MP3"])

        with pytest.raises(ProviderError):
            synth.synthesize("hello", "v")
        assert len(synth.sessions) == 1

    def test_no_audio_is_not_retried(self):
        synth = _ScriptedSynthesizer([b"", b"MP3"])

        with pytest.raises(ProviderError, match="returned no audio"):
            synth.synthesize("hello", "v")
        assert len(synth.sessions) == 1


class TestUpgradeStatus:
    @pytest.mark.parametrize(
        ("line", "status"),
        [
            ("HTTP/1.1 403 Forbidden", 403),
            ("HTTP/1.1 503 Service Unavailable", 503),
            ("garbage", None),
        ],
    )
    def test_the_status_code_is_read_from_the_status_line(self, line, status):
        assert WebSocketClient._status_code(line) == status
