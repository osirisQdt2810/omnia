"""Where a batch reports how far it has got — and therefore whether Anki is usable meanwhile.

One batch runner, three places its progress can go, and the choice is the whole feature:

* :class:`ModalDialog` — Anki's own progress window. It is ``ApplicationModal``, so while it is
  up the reviewer accepts no input. Right for an operation the user must wait for.
* :class:`BackgroundBar` — a :class:`~omnia.core.progress.JobTracker` published under the
  plugin's progress service, drawn by whoever is looking (today, the Smart Notes card in the
  settings page). Nothing is modal, so the user carries on studying while their cards generate.
* :class:`Silent` — nothing at all.

Splitting this out of :mod:`~omnia.plugins.smart_notes.integration.batch` is what makes the
choice a caller's rather than a branch: ``batch.py`` calls four methods and never asks which
surface it has. It is also what let the background mode arrive without touching the cohort /
round / wave logic, which is the part that would be expensive to get wrong.

The counting is NOT here. The driver already keeps the only authoritative count (it advances at
commit, on one thread), and a surface that counted for itself would be a second answer to the
same question.
"""

from __future__ import annotations

from typing import Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.progress import JobTracker

logger = get_logger("smart_notes")


class ProgressSurface:
    """What a batch can say about itself, and the one question it asks back.

    The base is the do-nothing implementation rather than an abstract class on purpose: a
    surface that has not implemented one of these is silent about it, never broken. A batch
    must not fail because the thing drawing it does not care.
    """

    def start(self, total: int) -> None:
        """A run of ``total`` notes is beginning."""

    def publish(self, done: int, total: int) -> None:
        """``done`` of ``total`` notes have been committed."""

    def hold(self, reason: str) -> None:
        """Say why the job is deliberately not progressing; ``""`` means it is running again.

        A job that is WAITING looks identical to a stuck one from outside, and the difference
        decides whether somebody presses Stop on work that was going to finish by itself.
        """

    def finish(self) -> None:
        """The run is over — successfully, by failure, or by cancellation."""

    def cancelled(self) -> bool:
        """Whether the user has asked to stop. Read between cohorts, never mid-note."""
        return False


class Silent(ProgressSurface):
    """Say nothing. What background auto-generation used before it had anywhere to say it."""


class ModalDialog(ProgressSurface):
    """Anki's progress window: a label, a bar and a Cancel button, over everything else.

    Every call is guarded, and each guard is load-bearing rather than defensive habit:

    * ``start`` incremented Anki's GLOBAL progress refcount, so a raise anywhere between it and
      ``finish`` leaks it for the rest of the session — no dialog ever opens again, in any
      add-on. ``finish`` therefore cannot be allowed to raise either.
    * ``publish`` runs on the Qt main thread, where Anki has no try/except of its own around
      posted closures, so an exception there becomes a traceback dialog about a background job
      the user never asked about. A cosmetic count is not worth that.
    """

    def start(self, total: int) -> None:
        anki_compat.progress_start(f"Omnia: generating… (0/{total})", total)

    def hold(self, reason: str) -> None:
        if not reason:
            return
        anki_compat.progress_label(f"Omnia: {reason}")

    def publish(self, done: int, total: int) -> None:
        def show() -> None:
            try:
                anki_compat.progress_update(
                    f"Omnia: generating… ({done}/{total})", done, total
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("smart_notes: progress update failed")

        anki_compat.run_on_main(show)

    def finish(self) -> None:
        try:
            anki_compat.progress_finish()
        except Exception:  # pragma: no cover - defensive
            logger.exception("smart_notes: closing the progress dialog failed")

    def cancelled(self) -> bool:
        return bool(anki_compat.progress_was_cancelled())


class BackgroundBar(ProgressSurface):
    """A tracker anyone can read, and no window anybody has to wait for.

    Args:
        tracker: Published under the plugin's progress service for as long as the batch runs.

    No main-thread hop: writing a tracker is a lock and two integers, where ``ModalDialog`` has
    to reach Qt. That difference is why this can publish on every commit while the dialog has to
    coalesce — and why a long batch costs the main thread nothing at all.
    """

    def __init__(self, tracker: JobTracker) -> None:
        self._tracker = tracker

    def start(self, total: int) -> None:
        self._tracker.start(total)

    def publish(self, done: int, total: int) -> None:
        self._tracker.record(done)

    def hold(self, reason: str) -> None:
        self._tracker.hold(reason)

    def finish(self) -> None:
        self._tracker.finish()

    def cancelled(self) -> bool:
        return self._tracker.cancelled


def surface_for(*, background: bool, tracker: Optional[JobTracker]) -> ProgressSurface:
    """The surface a user-started batch should report to.

    Falls back to the dialog when background mode is asked for with no tracker to write to —
    silently dropping the progress of a batch the user STARTED would leave them with no way to
    tell it apart from nothing having happened.
    """
    if background and tracker is not None:
        return BackgroundBar(tracker)
    return ModalDialog()
