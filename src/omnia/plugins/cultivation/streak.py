"""Day streaks with one free freeze per rolling 7 days (spec §2; Lally et al. 2010).

Pure: a set of active days in, a :class:`Streak` out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

#: Window in which at most one missed day is forgiven (spec §2 / §3).
FREEZE_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Streak:
    """A streak as the status card shows it.

    Attributes:
        days: Active days in the current run (freeze days bridge the run but do not count).
        freeze_ready: True when no freeze was spent in the last 7 days.
        active_today: Whether today already counts.
    """

    days: int
    freeze_ready: bool
    active_today: bool


def compute_streak(active_days: Iterable[date], today: date) -> Streak:
    """Walk back from today counting active days, bridging one gap per rolling 7 days.

    Today itself never breaks the streak — the day is still in progress.
    """
    active = set(active_days)
    count = 0
    freezes: list[date] = []
    day = today if today in active else today - timedelta(days=1)
    while True:
        if day in active:
            count += 1
        else:
            # A freeze only forgives a single missed day that the run continues past; a gap the
            # streak ends at spends nothing, so the freeze is still there for the next miss.
            recent = any((f - day).days < FREEZE_WINDOW_DAYS for f in freezes)
            if recent or (day - timedelta(days=1)) not in active:
                break
            freezes.append(day)
        day -= timedelta(days=1)
    ready = not any((today - f).days < FREEZE_WINDOW_DAYS for f in freezes)
    return Streak(days=count, freeze_ready=ready, active_today=today in active)
