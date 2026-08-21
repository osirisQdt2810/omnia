"""A process-wide bound on how many provider requests may be in flight at once.

The quota being protected belongs to the **provider account**, not to any one feature, dialog
or unit of work, so exactly one :class:`ProviderLimiter` exists and every path that reaches a
provider shares it — background fan-outs and interactive calls alike. A bound owned per feature,
per hub or per dialog would let two of them each open their own N connections against the same
account, which is the one thing the account cannot tell apart from abuse.

This module deliberately knows nothing about HTTP. The limiter is a plain counting gate over
"units of provider work", so the decorator that spends a permit
(:class:`~omnia.core.network.http.ThrottledHttpClient`) can live next to the transport it
decorates without either module importing the other.

**What it does NOT cover**, stated here because a safety bound with an undocumented hole is
worse than no bound: anything that reaches the network without going through an
:class:`~omnia.core.network.http.HttpClient`. Three things, and all three are named so that the
next reader auditing coverage does not have to trust a list that was true once.

* ``edge_tts`` speaks a raw WebSocket. It is NOT exempt — it takes a permit explicitly around
  each synthesis chunk (see
  :class:`~omnia.core.providers.tts.edge_tts.EdgeProtocolSynthesizer`).
* The piper voice-model download reaches for a file with ``urllib`` directly and takes no permit
  (see :mod:`omnia.core.providers.tts.voice_models`). Deliberate and harmless: it is a one-off
  fetch from a model host, not a request against the provider account this bound protects.
* User-authored Python may ``import urllib`` and call anything it likes, and nothing here can
  see it. Work made mostly of user-authored code is bounded by the worker count alone.

Pure stdlib ``threading`` — no ``aqt``/``anki``, no network — so it unit-tests headless.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# The RESTING capacity: what the limiter allows when no generation fan-out is running.
#
# It is deliberately the widest fan-out this build will ever start (the worker ceiling), which
# makes the resting limiter inert — and that is the point. At rest the only provider calls are
# interactive ones a person triggered (an Account credit fetch, a voice-list refresh, a Test
# key), and those were concurrent with each other before this module existed. A resting
# capacity of 1 would have SERIALISED them process-wide: a bound nobody asked for, on work that
# was never the reason a bound was wanted.
#
# The bound bites where the fan-out is: ``pooled_dispatch`` narrows the capacity for the
# duration of a run and restores it afterwards.
DEFAULT_CAPACITY = 16


@dataclass(frozen=True)
class LimiterStats:
    """What the limiter observed, for the benchmark and for diagnosing a slow run.

    ``peak_in_flight`` is the evidence that the bound actually bound; ``total_wait_seconds``
    separates "the provider is slow" from "our own bound is the queue" — without it a run that
    got slower after raising N looks identical to one that got slower because the API did.
    """

    acquired: int
    peak_in_flight: int
    total_wait_seconds: float


class ProviderLimiter:
    """Bounds how many provider requests are in flight process-wide.

    Capacity is re-settable at runtime because it is a user setting that must take effect
    without an Anki restart: raising it wakes as many waiters as the new headroom allows,
    lowering it never interrupts work already in flight — the surplus simply drains.

    Implemented over a :class:`threading.Condition` and an integer rather than a
    :class:`threading.Semaphore`, because resizing a semaphore is not expressible and because
    the peak/wait instrumentation has to be read under the same lock as the counter to mean
    anything. CPython wakes ``Condition`` waiters in FIFO order, which is the fairness we want
    (no worker starves behind newer arrivals); that is an implementation property of CPython,
    not a language guarantee, and nothing here depends on it for correctness.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._condition = threading.Condition()
        self._capacity = max(1, int(capacity))
        self._in_flight = 0
        self._acquired = 0
        self._peak_in_flight = 0
        self._total_wait = 0.0
        # Narrowing state (see :meth:`narrowed`): how many blocks are currently holding the
        # bound down, and what to put back when the last of them leaves.
        self._narrow_depth = 0
        self._resting_capacity = self._capacity

    @property
    def capacity(self) -> int:
        """The number of requests allowed in flight at once."""
        with self._condition:
            return self._capacity

    def set_capacity(self, capacity: int) -> int:
        """Re-bound the limiter to ``capacity`` (clamped to at least 1); return the OLD value.

        Clamped rather than validated: this value arrives from user config, and a limiter that
        raised on a nonsense number would turn a bad setting into a feature that never runs.

        The previous capacity is returned so the caller that narrowed the bound can put it back
        (:func:`~omnia.core.concurrency.pool.pooled_dispatch` does, in a ``finally``). Leaving a
        run's capacity behind would make the limiter's state depend on which fan-out happened to
        run last in the session.
        """
        with self._condition:
            previous = self._capacity
            self._set_capacity_locked(capacity)
            return previous

    def _set_capacity_locked(self, capacity: int) -> None:
        """Assign the capacity with :attr:`_condition` already held."""
        self._capacity = max(1, int(capacity))
        # Raising capacity must release the difference immediately, or the workers already
        # parked would sit there until an unrelated request happened to finish.
        self._condition.notify_all()

    @contextmanager
    def narrowed(self, capacity: int) -> Iterator[None]:
        """Hold the bound at ``capacity`` — or tighter — for the duration of the block.

        The limiter owns its own capacity, so it also owns putting it back. A caller doing
        ``previous = set_capacity(n)`` and restoring in a ``finally`` is correct exactly once:
        two overlapping blocks each restore the value THEY happened to read, and the
        process-wide bound ends up at whatever the last one saw — quietly, and for the rest of
        the session. Nothing can overlap today, because every generation path goes through a
        ``QueryOp`` on Anki's single-worker collection executor, but that is an invariant of
        aqt's internals rather than of this module, and the cost of not depending on it is one
        counter.

        A nested block never WIDENS an outer one: while both are active the tighter bound wins,
        because the outer block's reason for narrowing has not gone away.
        """
        with self._condition:
            if self._narrow_depth == 0:
                self._resting_capacity = self._capacity
            self._narrow_depth += 1
            self._set_capacity_locked(min(self._capacity, max(1, int(capacity))))
        try:
            yield
        finally:
            with self._condition:
                self._narrow_depth -= 1
                if self._narrow_depth == 0:
                    self._set_capacity_locked(self._resting_capacity)

    @contextmanager
    def permit(self) -> Iterator[None]:
        """Hold one permit for the duration of the ``with`` block."""
        self._acquire()
        try:
            yield
        finally:
            self._release()

    @property
    def stats(self) -> LimiterStats:
        """A consistent snapshot of what the limiter has seen since the last reset."""
        with self._condition:
            return LimiterStats(
                acquired=self._acquired,
                peak_in_flight=self._peak_in_flight,
                total_wait_seconds=self._total_wait,
            )

    def reset_stats(self) -> None:
        """Zero the counters (the benchmark measures one configuration at a time)."""
        with self._condition:
            self._acquired = 0
            self._peak_in_flight = self._in_flight
            self._total_wait = 0.0

    def _acquire(self) -> None:
        with self._condition:
            if self._in_flight >= self._capacity:
                # Timed from INSIDE the check, so the counter is the time spent parked for a
                # permit and nothing else. Started before acquiring the lock, it also charged
                # every uncontended acquire a few microseconds of lock handoff — and a "was our
                # own bound the queue?" signal that is never zero cannot answer the question.
                started = time.monotonic()
                while self._in_flight >= self._capacity:
                    self._condition.wait()
                self._total_wait += time.monotonic() - started
            self._in_flight += 1
            self._acquired += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    def _release(self) -> None:
        with self._condition:
            self._in_flight -= 1
            self._condition.notify()


# The one limiter. Module-level because the thing it protects — the provider account's rate
# limit — is itself process-wide; see the module docstring for why per-hub is wrong.
PROVIDER_LIMITER = ProviderLimiter()
