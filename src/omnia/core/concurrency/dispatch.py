"""The seam that decides HOW a batch of independent work units is run — and nothing else.

A caller knows which units may run together; it must not know whether they are run one after
another, on a thread pool, or on something else entirely. Threads belong to the Anki glue, which
owns the ``QueryOp`` a pool must not outlive — so the concrete pooled implementation lives in
:mod:`omnia.core.concurrency.pool`, and this module stays importable with no
``concurrent.futures`` and no ``aqt``.

The rule that buys is about POLICY, not about the word ``threading``: a pure-logic layer may
depend on this protocol without thereby deciding how many threads exist, or touching Anki. A
lock over its own state is fine; owning a ``ThreadPoolExecutor`` is not. See the amendment on
ADR-016, which used to state a stricter rule than the tree actually satisfies.

The default (:data:`SEQUENTIAL_DISPATCH`) runs everything in the calling thread, in order: a
caller's behaviour is deterministic unless it explicitly asks for otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar, Union

T = TypeVar("T")


class Dispatch(Protocol):
    """Runs a batch of independent, argument-less work units."""

    def run(self, units: Sequence[Callable[[], T]]) -> list[Union[T, Exception]]:
        """Run every unit and return one entry per unit, **in input order**.

        Input order, never completion order: the caller pairs the results back onto the work it
        submitted by position, and a list that arrived in the order things happened to finish
        would silently attribute one unit's output to another.

        An exception raised by a unit is RETURNED in that unit's slot rather than propagated.
        One unit's failure must not discard the siblings that already succeeded, and must not be
        attributed to units that never ran — a caller reporting "the whole selection failed" from
        one raised exception is the outcome this returns-not-raises contract exists to prevent.
        """
        ...  # pragma: no cover - protocol


class SequentialDispatch:
    """Runs the units one at a time in the calling thread. The default everywhere.

    Keeps a caller that never asked for concurrency byte-identical to what it did before this
    seam existed, and keeps a batch of width 1 (the common case on an interactive path) free of
    any pool at all.
    """

    def run(self, units: Sequence[Callable[[], T]]) -> list[Union[T, Exception]]:
        """Run ``units`` in order, capturing each unit's exception in its own slot."""
        outcomes: list[Union[T, Exception]] = []
        for unit in units:
            try:
                outcomes.append(unit())
            except Exception as exc:  # returned, not raised — see Dispatch.run
                outcomes.append(exc)
        return outcomes


# Stateless, so one shared instance is safe everywhere and no caller has to construct one.
SEQUENTIAL_DISPATCH = SequentialDispatch()
