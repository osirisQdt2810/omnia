"""How far a long-running background job has got — as data, published by NAME.

Why this exists
---------------
Anki has exactly one progress surface built in, and it is ``ApplicationModal``: while it is up,
no other window accepts input, including the reviewer. That is correct for an operation the user
must wait for, and wrong for one they started precisely so they could do something else. A batch
that generates two hundred cards is the second kind — it is minutes of provider round trips, and
the whole point of running it in the background is to carry on studying meanwhile.

The alternative to a modal dialog is not "no progress", it is progress somewhere that does not
take the screen. So a job publishes a :class:`JobTracker` under
``progress_service("<plugin_id>")`` while it runs, and whoever is drawing reads it whenever they
draw. Two consequences that are the point rather than side effects:

* **Nothing calls into the UI.** A window that has been closed cannot be called back into, and a
  job that outlives three openings of the settings dialog needs no bookkeeping about which one is
  current. The reader polls; the job never knows a reader exists.
* **Any plugin gets the same treatment for free.** The settings page asks every plugin card for
  ``<id>.progress`` and renders whatever answers. Sync already worked this way against its own
  bespoke job; this is that idea with the plugin-specific half removed.

The shape is deliberately COUNTED (``done`` of ``total``) rather than fractional. A batch knows
how many notes it was given, and "142 of 300" is the answer a person actually wants — where a
percentage alone hides whether the remaining 30% is three notes or three hundred.

Pure: no ``aqt``, no threads of its own, no clock. The tracker is mutated from a worker thread
and read from the Qt main thread, which is the one thing here that needs a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

#: How a plugin's progress is found in the service registry. A convention rather than a
#: registration API: the settings page asks for it by name for every plugin it draws, and a
#: plugin with nothing to report simply never provides it (``lookup`` answers ``None``).
_PROGRESS_SUFFIX = ".progress"


def progress_service(plugin_id: str) -> str:
    """The service name a plugin publishes its :class:`JobTracker` under."""
    return f"{plugin_id}{_PROGRESS_SUFFIX}"


@dataclass(frozen=True)
class JobProgress:
    """A snapshot of one running job, ready to render.

    Frozen, and handed out by value: the reader is on a different thread from the writer, and a
    live view would let a bar draw ``done`` from after a ``total`` it does not match.

    Attributes:
        done: How many units are finished.
        total: How many there are, or 0 when that is not yet known.
        label: What the job is doing, in words, for a tooltip.
        active: Whether it is still running. A finished job keeps its counts so the last frame
            drawn is the complete one rather than an empty bar.
        cancelled: Whether a stop was asked for. Distinct from ``not active``: a job that has
            been asked to stop is still running until it reaches a point where stopping is safe.
    """

    done: int = 0
    total: int = 0
    label: str = ""
    active: bool = False
    cancelled: bool = False

    @property
    def percent(self) -> Optional[float]:
        """How far along, 0–100, or ``None`` when the size is not yet known.

        ``None`` rather than 0.0, for the reason a bar pinned at zero is worse than no bar: it
        reads as stuck. The caller shows an indeterminate state instead.
        """
        if self.total <= 0:
            return None
        return min(100.0, max(0.0, 100.0 * self.done / self.total))

    def summary(self) -> str:
        """One line for a tooltip: what it is doing and how far it has got."""
        if not self.active and not self.done:
            return ""
        counted = f"{self.done} of {self.total}" if self.total > 0 else str(self.done)
        if self.cancelled and self.active:
            return f"Stopping… ({counted})"
        if not self.active:
            return f"{self.label} — {counted}" if self.label else counted
        return f"{self.label} {counted}" if self.label else counted


class JobTracker:
    """A job's progress, written by the job and read by whoever is drawing.

    Args:
        label: What the job is doing, in words. Shown in the tooltip beside the count.

    The counter is monotonic by construction — :meth:`advance` only ever adds — so a bar built
    from it cannot go backwards, which is the failure mode of counting overlapping work by
    polling the workers instead of by what has committed.
    """

    def __init__(self, label: str = "") -> None:
        self._lock = threading.Lock()
        self._label = label
        self._done = 0
        self._total = 0
        self._active = False
        self._cancelled = False

    def start(self, total: int) -> None:
        """Begin a run of ``total`` units, discarding any previous one's counts."""
        with self._lock:
            self._done = 0
            self._total = max(0, int(total))
            self._active = True
            self._cancelled = False

    def advance(self, count: int = 1) -> None:
        """Record ``count`` more finished units."""
        if count <= 0:
            return
        with self._lock:
            self._done += count

    def record(self, done: int) -> None:
        """Record that ``done`` units in total are finished.

        Beside :meth:`advance` rather than instead of it, because the two callers count
        differently and both are right: a worker that finishes one unit knows an increment, and
        a driver that already keeps its own running total knows an absolute. Clamped upward so
        either route is monotonic — an absolute that arrives out of order (two publishes racing
        on the way to the reader) cannot walk the bar backwards.
        """
        with self._lock:
            self._done = max(self._done, int(done))

    def finish(self) -> None:
        """Mark the run over. The counts are KEPT, so the last frame drawn is the full one."""
        with self._lock:
            self._active = False

    def cancel(self) -> None:
        """Ask the job to stop. It is up to the job to notice, at a point where that is safe."""
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether a stop has been asked for — read by the job, between units of work."""
        with self._lock:
            return self._cancelled

    @property
    def active(self) -> bool:
        """Whether a run is in flight."""
        with self._lock:
            return self._active

    def snapshot(self) -> JobProgress:
        """Everything a renderer needs, consistent as of this moment."""
        with self._lock:
            return JobProgress(
                done=self._done,
                total=self._total,
                label=self._label,
                active=self._active,
                cancelled=self._cancelled,
            )
