"""Tests for the process-wide provider limiter and the HTTP decorator that spends its permits.

The limiter is the only thing standing between N concurrent workers and a systematic 429, so
these tests assert the two properties that matter — the bound actually bounds, and the permit
is held across a retry's backoff — plus the one property nobody notices until it breaks: the
decorator must add ZERO requests and ZERO sleeps to what the inner client would have done.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omnia.core.network.http import (
    RetryPolicy,
    ThrottledHttpClient,
    UrllibHttpClient,
)
from omnia.core.network.limiter import ProviderLimiter

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"


class _Resp:
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _CountingHttp:
    """A fake HttpClient recording how many requests overlap, and for how long."""

    def __init__(self, hold: float = 0.02) -> None:
        self._hold = hold
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def _work(self):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(self._hold)
        with self._lock:
            self.in_flight -= 1
        return {}

    def post_json(self, url, payload, *, headers=None):
        return self._work()

    def post_form(self, url, fields, *, headers=None):
        return self._work()

    def post_json_for_bytes(self, url, payload, *, headers=None):
        self._work()
        return b""

    def get_bytes(self, url, *, params=None, headers=None):
        self._work()
        return b""

    def get_json(self, url, *, params=None, headers=None):
        return self._work()


def _drive(client, count: int) -> None:
    """Fire ``count`` requests at ``client`` from ``count`` threads and wait for all of them."""
    threads = [
        threading.Thread(target=lambda: client.post_json("https://x", {}))
        for _ in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class TestProviderLimiter:
    def test_capacity_bounds_concurrent_requests(self):
        inner = _CountingHttp()
        client = ThrottledHttpClient(inner, ProviderLimiter(2))

        _drive(client, 8)

        assert inner.calls == 8
        assert inner.peak == 2  # never more in flight than the bound

    def test_a_capacity_of_one_serialises_everything(self):
        inner = _CountingHttp()
        client = ThrottledHttpClient(inner, ProviderLimiter(1))

        _drive(client, 5)

        assert inner.peak == 1

    def test_capacity_can_be_raised_at_runtime(self):
        # A setting must take effect without an Anki restart, and raising it has to WAKE the
        # workers already parked — not leave them until an unrelated request happens to finish.
        limiter = ProviderLimiter(1)
        inner = _CountingHttp(hold=0.05)
        client = ThrottledHttpClient(inner, limiter)
        threads = [
            threading.Thread(target=lambda: client.post_json("https://x", {}))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.02)
        limiter.set_capacity(4)
        for thread in threads:
            thread.join()

        assert inner.peak > 1

    def test_capacity_can_be_lowered_at_runtime(self):
        limiter = ProviderLimiter(4)
        limiter.set_capacity(1)
        inner = _CountingHttp()
        client = ThrottledHttpClient(inner, limiter)

        _drive(client, 4)

        assert inner.peak == 1

    def test_a_nonsense_capacity_is_clamped_not_rejected(self):
        # The value arrives from user config; a limiter that raised would turn a bad setting
        # into a feature that never runs.
        assert ProviderLimiter(0).capacity == 1
        limiter = ProviderLimiter(3)
        limiter.set_capacity(-5)
        assert limiter.capacity == 1

    def test_stats_report_the_peak_and_the_wait(self):
        limiter = ProviderLimiter(1)
        client = ThrottledHttpClient(_CountingHttp(hold=0.01), limiter)

        _drive(client, 3)

        stats = limiter.stats
        assert stats.acquired == 3
        assert stats.peak_in_flight == 1
        assert stats.total_wait_seconds > 0  # the bound, not the API, was the queue

    def test_an_uncontended_run_reports_no_wait_at_all(self):
        """Zero must be reachable, or the "was our own bound the queue?" signal says nothing.

        Timed from outside the capacity check, every acquire charged a few microseconds of lock
        handoff and the counter was never zero — so a run that queued behind the bound looked
        exactly like one that never waited, only with a bigger number.
        """
        limiter = ProviderLimiter(8)
        client = ThrottledHttpClient(_CountingHttp(), limiter)

        _drive(client, 3)

        assert limiter.stats.acquired == 3
        assert limiter.stats.total_wait_seconds == 0.0

    def test_a_nested_narrowing_restores_the_resting_capacity_once(self):
        """Two overlapping narrowings must leave the process-wide bound where they found it.

        Save-and-restore in the CALLER cannot do this: each block puts back the value it read,
        so the inner exit restores the outer block's number and the outer exit restores the
        inner's. Nothing overlaps today; the counter is what stops that being an assumption
        about aqt's scheduling.
        """
        limiter = ProviderLimiter(16)

        with limiter.narrowed(8):
            assert limiter.capacity == 8
            with limiter.narrowed(3):
                assert limiter.capacity == 3
            # The outer block's reason for narrowing has not gone away.
            assert limiter.capacity == 3
        assert limiter.capacity == 16

    def test_a_nested_narrowing_never_widens_the_outer_one(self):
        limiter = ProviderLimiter(16)

        with limiter.narrowed(2), limiter.narrowed(9):
            assert limiter.capacity == 2
        assert limiter.capacity == 16


class TestPermitLifetime:
    def test_the_permit_is_held_across_a_retry_backoff(self):
        """Releasing during the sleep would let the pool refill inside the 429's window.

        The limiter prevents a SYSTEMATIC 429; retry absorbs an occasional one. If the permit
        were released while backing off, a second request would start during the very
        rate-limit window that produced the first 429 and turn one into a storm.
        """
        limiter = ProviderLimiter(1)
        started: list[str] = []
        gate = threading.Event()

        class _Slow:
            def post_json(self, url, payload, *, headers=None):
                started.append(url)
                gate.wait(0.5)  # stands in for RetryPolicy's backoff sleep
                return {}

            post_form = post_json_for_bytes = get_bytes = get_json = post_json

        client = ThrottledHttpClient(_Slow(), limiter)
        first = threading.Thread(target=lambda: client.post_json("a", {}))
        second = threading.Thread(target=lambda: client.post_json("b", {}))
        first.start()
        time.sleep(0.05)
        second.start()
        time.sleep(0.05)

        assert started == ["a"]  # "b" is still waiting for the permit "a" holds
        gate.set()
        first.join()
        second.join()
        assert started == ["a", "b"]


class TestThrottledClientIsTransparent:
    """The decorator must change nothing about what the inner client does."""

    def _client(self, sleeps: list[float]) -> ThrottledHttpClient:
        retry = RetryPolicy(
            max_attempts=3, base_delay=1.0, jitter=lambda: 0.0, sleep=sleeps.append
        )
        return ThrottledHttpClient(
            UrllibHttpClient(timeout=1, retry=retry), ProviderLimiter(1)
        )

    def test_it_adds_no_requests_and_no_sleeps_on_success(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            return _Resp(b'{"ok": true}')

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        sleeps: list[float] = []

        assert self._client(sleeps).post_json("https://x", {}) == {"ok": True}
        assert calls["n"] == 1
        assert sleeps == []

    def test_it_preserves_the_retry_schedule(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.HTTPError("https://x", 429, "slow down", {}, None)
            return _Resp(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        sleeps: list[float] = []

        self._client(sleeps).post_json("https://x", {})

        assert calls["n"] == 2
        assert sleeps == [1.0]  # exactly the undecorated policy's first backoff


class TestOneLimiterForEveryPath:
    def test_bulk_and_interactive_calls_contend_for_the_same_permits(self):
        """The bound belongs to the provider ACCOUNT, so one dialog cannot get its own.

        Two clients built independently — as the batch's provider and a settings-dialog
        provider are — must still share the process-wide limiter, or "3 at a time" silently
        becomes 3 per hub.
        """
        from omnia.core.network.limiter import PROVIDER_LIMITER

        inner = _CountingHttp()
        bulk = ThrottledHttpClient(inner, PROVIDER_LIMITER)
        interactive = ThrottledHttpClient(inner, PROVIDER_LIMITER)
        previous = PROVIDER_LIMITER.capacity
        PROVIDER_LIMITER.set_capacity(1)
        try:
            threads = [
                threading.Thread(target=lambda c=client: c.post_json("https://x", {}))
                for client in (bulk, interactive, bulk, interactive)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            PROVIDER_LIMITER.set_capacity(previous)

        assert inner.peak == 1

    def test_the_default_http_client_is_throttled(self):
        # Providers capture DEFAULT_HTTP_CLIENT by value at construction, so this is the one
        # chance to put the limiter in the path — a later rebind would be invisible to them.
        from omnia.core.network.http import DEFAULT_HTTP_CLIENT

        assert isinstance(DEFAULT_HTTP_CLIENT, ThrottledHttpClient)

    def test_edge_tts_takes_a_permit_even_though_it_speaks_websocket(self):
        """The one shipped provider that never touches an HttpClient must not be exempt.

        Edge is a raw WebSocket (the maintained ``edge-tts`` package needs compiled aiohttp and
        cannot be vendored), so ``ThrottledHttpClient`` never sees it. Left unbounded it is
        precisely the provider most likely to answer a burst by closing the connection — and its
        failures never reach ``RetryPolicy`` either, so nothing downstream absorbs them.
        """
        from omnia.core.network.limiter import PROVIDER_LIMITER
        from omnia.core.providers.tts.edge_tts import EdgeProtocolSynthesizer

        synth = EdgeProtocolSynthesizer()
        before = PROVIDER_LIMITER.stats.acquired
        held: list[int] = []

        def _one_session(text, voice):
            held.append(PROVIDER_LIMITER.stats.acquired)
            return b"MP3"

        synth._synthesize_one_session = _one_session  # type: ignore[method-assign]
        synth.synthesize("hello", "en-US-AriaNeural")

        # A permit was taken BEFORE the session opened, and released after it closed.
        assert held == [before + 1]


class TestImportOrderCannotDeadlockTheImportSystem:
    """``core/network`` must be importable FIRST, from a cold interpreter, in any order."""

    def test_importing_the_limiter_does_not_drag_in_the_provider_package(self):
        """The cycle this pins: ``http`` raises a ``ProviderError``, so it imports
        ``core.providers`` — whose concrete providers import ``core.network.http`` right back.

        While ``core/network/__init__`` imported its submodules eagerly, ``import
        omnia.core.network.limiter`` — a module with no dependencies whatsoever — ran that whole
        loop and died with a bare ``ImportError`` on whichever half was still half-built. It only
        stayed hidden because every import in the tree happened to reach ``core.providers``
        first. Asserted in a COLD interpreter, because in this one everything is already
        imported and nothing can fail.
        """
        script = "import omnia.core.network.limiter as m; print(m.DEFAULT_CAPACITY)"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_SRC), str(_REPO_ROOT / "vendor" / "universal")]
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "16"


class TestNoDeadlock:
    def test_a_json_get_spends_exactly_one_permit(self):
        """``get_json`` delegates to the INNER client's ``get_bytes``, not the decorator's.

        Spending two permits for one round trip would deadlock at capacity 1 — the classic way
        a decorated client bites.
        """
        limiter = ProviderLimiter(1)
        inner = _CountingHttp(hold=0)
        client = ThrottledHttpClient(inner, limiter)

        client.get_json("https://x")

        assert limiter.stats.acquired == 1


@pytest.mark.parametrize("capacity", [1, 2, 5])
class TestCapacityIsExact:
    def test_the_peak_reaches_but_never_exceeds_capacity(self, capacity):
        inner = _CountingHttp(hold=0.02)
        client = ThrottledHttpClient(inner, ProviderLimiter(capacity))

        _drive(client, capacity * 3)

        assert inner.peak == capacity
