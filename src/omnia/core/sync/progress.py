"""How far along a pull is, and how much longer it has — as data, not as a widget.

A pull moves an ``.apkg`` with the media inside it, and media is the bulk: a few chosen decks can
be hundreds of megabytes. That is long enough that the user must be able to put the window away
and carry on studying, and long enough that "it is working" is not a good enough answer — they
want a number and a time.

So progress is a value, computed here and rendered in three places that must agree: the picker's
own bar, the Sync button quietly filling up behind its label, and the tooltip on that button. Put
the arithmetic in any one of them and the other two drift.

Two things it deliberately does NOT do:

* **Guess.** Before the source has said how big the package is there is no percentage, and
  :attr:`Progress.fraction` is ``None`` rather than 0 — a bar sitting at 0% for forty seconds
  while a collection is exported reads as stuck, and a bar that crawls to 90% and waits reads as
  lying. The phase says what is happening instead.
* **Average over the whole run.** The estimate uses the RECENT rate, because the two halves of a
  pull run at different speeds — a LAN download after a slow export would otherwise be estimated
  at the export's pace for most of its length.

Pure: no ``aqt``, no sockets, no clock of its own (the clock is injected, so the tests are not
timing-dependent).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

#: What a pull is doing. Ordered, and the order is what the UI shows as "step 2 of 4".
PREPARING = "preparing"
DOWNLOADING = "downloading"
IMPORTING = "importing"
DONE = "done"
FAILED = "failed"

PHASES: tuple[str, ...] = (PREPARING, DOWNLOADING, IMPORTING, DONE)

#: Words for each phase. Written from the TARGET's point of view — this is the machine the user
#: is sitting at — and naming the other machine where it is the one doing the work.
_LABELS = {
    PREPARING: "Packing it up on the other computer…",
    DOWNLOADING: "Copying it over…",
    IMPORTING: "Adding it to this collection…",
    DONE: "Done.",
    FAILED: "Stopped.",
}

#: How many recent samples the rate is averaged over. Enough to ride out a stalled second, few
#: enough that the estimate follows a real change in speed within a few seconds.
_WINDOW = 12

#: An estimate is not shown until there is enough to base one on. Two samples is a rate; it is
#: not yet an estimate anybody should read.
_MIN_SAMPLES = 4


@dataclass
class Progress:
    """A snapshot of one pull, ready to render."""

    phase: str = PREPARING
    done_bytes: int = 0
    total_bytes: Optional[int] = None
    eta_seconds: Optional[float] = None
    detail: str = ""

    @property
    def label(self) -> str:
        """The sentence for this phase."""
        return _LABELS.get(self.phase, "")

    @property
    def fraction(self) -> Optional[float]:
        """How far along, 0.0–1.0, or None when that is not yet knowable.

        None rather than 0.0 on purpose: a bar pinned at zero while a large collection is packed
        reads as stuck, and the caller can show a moving indeterminate bar instead.
        """
        if self.phase in (DONE,):
            return 1.0
        if not self.total_bytes:
            return None
        return min(1.0, max(0.0, self.done_bytes / self.total_bytes))

    @property
    def percent(self) -> Optional[int]:
        """The whole-number percentage, or None.

        Floored, never rounded: 99.7% shown as 100% beside a bar that is still moving is the one
        number a reader will call a lie.
        """
        fraction = self.fraction
        return None if fraction is None else int(fraction * 100)

    def summary(self) -> str:
        """One line for a tooltip: how far, and how much longer."""
        parts = [self.label]
        if self.percent is not None and self.phase not in (DONE, FAILED):
            parts.append(f"{self.percent}%")
        if self.eta_seconds is not None and self.phase not in (DONE, FAILED):
            parts.append(f"about {format_duration(self.eta_seconds)} left")
        if self.detail:
            parts.append(self.detail)
        return " · ".join(part for part in parts if part)


class ProgressTracker:
    """Turns a stream of "n bytes so far" into a percentage and an estimate.

    Args:
        clock: Returns a monotonically increasing number of seconds. Injected so the tests are
            not timing-dependent — an estimate asserted against a real clock is a flaky test.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque(maxlen=_WINDOW)
        self._phase = PREPARING
        self._done = 0
        self._total: Optional[int] = None
        self._detail = ""

    def set_phase(self, phase: str, *, detail: str = "") -> None:
        """Move to a new phase. Byte counts restart, because they count different things."""
        self._phase = phase
        self._detail = detail
        self._samples.clear()
        self._done = 0
        if phase in (PREPARING, IMPORTING):
            self._total = None

    def set_total(self, total: Optional[int]) -> None:
        """How many bytes this phase will move, once the other machine has said."""
        self._total = total if total and total > 0 else None

    def advance(self, done_bytes: int) -> None:
        """Record progress: ``done_bytes`` is the running total for THIS phase, not a delta."""
        self._done = max(self._done, int(done_bytes))
        self._samples.append((self._clock(), self._done))

    def snapshot(self) -> Progress:
        """The current state, ready to render."""
        return Progress(
            phase=self._phase,
            done_bytes=self._done,
            total_bytes=self._total,
            eta_seconds=self._eta(),
            detail=self._detail,
        )

    def _eta(self) -> Optional[float]:
        """Seconds remaining at the RECENT rate, or None when there is nothing to base it on.

        The recent rate rather than the average since the start: the phases run at different
        speeds — packing is disk-bound, copying is network-bound — and an average would spend
        most of the download quoting the export's pace.
        """
        if self._total is None or len(self._samples) < _MIN_SAMPLES:
            return None
        (first_at, first_bytes), (last_at, last_bytes) = (
            self._samples[0],
            self._samples[-1],
        )
        elapsed = last_at - first_at
        moved = last_bytes - first_bytes
        if elapsed <= 0 or moved <= 0:
            # Stalled. No estimate is better than one that jumps to infinity and back.
            return None
        remaining = self._total - last_bytes
        if remaining <= 0:
            return 0.0
        return remaining / (moved / elapsed)


def format_duration(seconds: float) -> str:
    """``"12 seconds"``, ``"3 minutes"``, ``"1 hour 5 minutes"`` — never ``"0:03:05"``.

    Rounded coarsely on purpose: an estimate to the second implies a precision it does not have,
    and a number that ticks 47, 52, 45 reads as broken rather than as honest.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return "a few seconds"
    if seconds < 90:
        return f"{round(seconds / 5.0) * 5} seconds"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{round(minutes)} minute" + ("" if round(minutes) == 1 else "s")
    hours, rest = divmod(round(minutes), 60)
    hour_part = f"{hours} hour" + ("" if hours == 1 else "s")
    return (
        hour_part
        if not rest
        else f"{hour_part} {rest} minute" + ("" if rest == 1 else "s")
    )


def format_bytes(count: Optional[int]) -> str:
    """``"742 MB"``. Decimal units, because that is what a download is usually quoted in."""
    if not count:
        return ""
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}" if size < 10 else f"{int(size)} {unit}"
        size /= 1000.0
    return ""
