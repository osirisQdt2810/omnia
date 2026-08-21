"""The threaded :class:`~omnia.core.concurrency.dispatch.Dispatch`, paired with the limiter.

Separated from the protocol next door because of what it imports, not because of what it does:
a pure-logic module may depend on the seam without pulling in ``concurrent.futures``. The
lifetime of a pool is also an Anki fact — it must be created inside a ``QueryOp``'s ``op()`` and
shut down before ``op()`` returns, because nothing may outlive the operation that owns it, and
the test harness runs ``op()`` inline, so a leaked pool would not fail a test; it would only
surface in the real app.

Two things this module is deliberately NOT:

* It is not where the provider bound lives. The pool decides how many units run at once; the
  :class:`~omnia.core.network.limiter.ProviderLimiter` decides how many provider REQUESTS are
  in flight, and one unit is not one request (a unit may ask a model to classify something
  before it does its real work, and a chain of fallbacks may call a provider several times
  before one produces). The two are wired together in :func:`pooled_dispatch`, which is the only
  place that knows both, and :func:`request_capacity` is where they are allowed to differ.
* It is not a general executor. It is handed leaf work only, and never submits to itself, so a
  worker can never wait on the pool it is running in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar, Union

from omnia import envs
from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH, Dispatch
from omnia.core.network.limiter import PROVIDER_LIMITER, ProviderLimiter

T = TypeVar("T")

# Qt threads are cheap, but a thread named after nothing is unfindable in a crash dump.
_THREAD_PREFIX = "omnia-dispatch"


class PooledDispatch:
    """Runs a batch's work units on a :class:`ThreadPoolExecutor`, results in INPUT order.

    Ordinary ``ThreadPoolExecutor.map`` would do most of this, but it re-raises the first
    exception and abandons the rest — which means one broken unit discarding every sibling that
    already succeeded, and one broken unit being reported as the whole batch failing. So futures
    are collected positionally and each exception is returned in its own slot.
    """

    def __init__(self, workers: int) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)), thread_name_prefix=_THREAD_PREFIX
        )

    def run(self, units: Sequence[Callable[[], T]]) -> list[Union[T, Exception]]:
        """Submit every unit, wait for all of them, and return their outcomes in order."""
        futures: list[Future[T]] = [self._pool.submit(unit) for unit in units]
        outcomes: list[Union[T, Exception]] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # returned, not raised — see Dispatch.run
                outcomes.append(exc)
        return outcomes

    def close(self) -> None:
        """Shut the pool down, waiting for anything still running."""
        self._pool.shutdown(wait=True)


def request_capacity(workers: int) -> int:
    """How many provider REQUESTS may be in flight while ``workers`` units run.

    Defaults to ``workers``: with one permit per request and one request at a time per unit,
    that is the tightest bound the pool alone already delivers, so the default changes nothing
    and costs nothing. **It is a default, not the definition** — that distinction is the reason
    the limiter is a separate mechanism and not a restatement of the pool width.

    ``OMNIA_MAX_CONCURRENT_REQUESTS`` overrides it, and the case it exists for is the one the
    pool cannot express: a workload where one unit is several calls (a classification followed
    by the real request; a grouped request plus the individual fallbacks it degrades into) wants
    a wide pool and a narrow provider bound — "run 8 units at once, but keep at most 3 requests
    in flight". Derived from the worker count, raising one necessarily raises the other, which is
    exactly the coupling the limiter was introduced to break.
    """
    override = int(envs.OMNIA_MAX_CONCURRENT_REQUESTS)
    if override > 0:
        return override
    return max(1, workers)


@contextmanager
def pooled_dispatch(
    workers: int, *, limiter: ProviderLimiter = PROVIDER_LIMITER
) -> Iterator[Dispatch]:
    """Yield a dispatch bounded to ``workers``, with the provider limiter narrowed to match.

    ``workers <= 1`` yields the sequential dispatch and starts no threads at all: the
    conservative default must cost nothing, not merely behave as if it did. The limiter is
    still narrowed, because "one unit at a time" is also "one request at a time" and an
    interactive call arriving mid-run must not make that two.

    Capacity is :func:`request_capacity`, NOT ``workers + 1``. The old ``+1`` was described as a
    reserved lane for an interactive call, and it was neither: no interactive path can run
    concurrently with a fan-out (every one of them goes through a ``QueryOp`` on Anki's
    single-worker collection executor), and a capacity one greater than the maximum number of
    permits anyone can hold is a bound that provably never binds. What is left is an honest
    bound — and one an interactive call, if Anki ever lets it overlap, must queue behind rather
    than be handed a private lane through, because the quota it would spend is the same quota.

    Restoring the capacity is the limiter's job, not this function's
    (:meth:`~omnia.core.network.limiter.ProviderLimiter.narrowed`): capacity is process-wide
    state, and a run that left its own value behind would make the bound depend on which fan-out
    happened to run last in the session.
    """
    with limiter.narrowed(request_capacity(workers)):
        if workers <= 1:
            yield SEQUENTIAL_DISPATCH
            return
        pool = PooledDispatch(workers)
        try:
            yield pool
        finally:
            # In a `finally` because the pool must not outlive the QueryOp's op(): a pool still
            # alive when op() returns keeps non-daemon threads (and the collection they were
            # handed) alive past the operation Anki thinks has ended.
            pool.close()
