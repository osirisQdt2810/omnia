"""The realm ladder and its state machine — shared by both domains (spec §2).

A realm is a discrete level 1–9 that changes ONLY through :class:`RealmMachine`:

* **Promotion** needs ``power >= T[L+1]``. A realm attained before is re-entered at once
  (relearning is faster than first learning — Nelson 1985); a NEW realm only becomes
  *eligible*, and is entered by passing a trial (Bloom's mastery learning).
* **Demotion** has hysteresis: below ``T[L]`` the realm is *unstable*, and only after
  ``demotion_buffer_days`` there does it drop to the highest realm the power still holds.

Pure: dates in, dates out. No ``aqt``/``anki``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from typing import Optional

#: Realm names, index 0 = L1 (spec §2, same for both domains).
REALM_NAMES: tuple[str, ...] = (
    "Luyện Khí",
    "Trúc Cơ",
    "Kim Đan",
    "Nguyên Anh",
    "Hóa Thần",
    "Luyện Hư",
    "Hợp Thể",
    "Đại Thừa",
    "Chân Tiên",
)
MAX_REALM = len(REALM_NAMES)

#: Status words the UI renders (wire format — the page switches on them).
STABLE = "stable"
UNSTABLE = "unstable"  # "bất ổn": below the realm's threshold, inside the buffer
ELIGIBLE = "eligible"  # may start a trial for the next realm
COOLDOWN = "cooldown"  # eligible, but a trial failed recently
PEAK = "peak"  # Chân Tiên — nothing above


@dataclass(frozen=True)
class RealmLadder:
    """The power threshold of each realm. ``thresholds[0]`` is L1's (``-inf`` = no floor)."""

    thresholds: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.thresholds) != MAX_REALM:
            raise ValueError(f"a ladder needs {MAX_REALM} thresholds")
        if any(b <= a for a, b in pairwise(self.thresholds)):
            raise ValueError("realm thresholds must be strictly increasing")

    def threshold(self, realm: int) -> float:
        """``T[L]`` for a 1-based realm."""
        return self.thresholds[realm - 1]

    def held_by(self, power: float) -> int:
        """The highest realm whose threshold ``power`` reaches (at least 1)."""
        held = 1
        for realm in range(1, MAX_REALM + 1):
            if power >= self.threshold(realm):
                held = realm
        return held

    def progress(self, realm: int, power: float) -> float:
        """Share (0–1) of the way from ``T[L]`` to ``T[L+1]`` — the goal-gradient bar."""
        if realm >= MAX_REALM:
            return 1.0
        low = self.threshold(realm)
        high = self.threshold(realm + 1)
        if math.isinf(low):
            low = 0.0
        if high <= low:
            return 1.0
        return max(0.0, min(1.0, (power - low) / (high - low)))


#: Anki thresholds in word-equivalents (spec §4.5).
ANKI_LADDER = RealmLadder((0, 1000, 2000, 3000, 5000, 8000, 10000, 14000, 17000))
#: DSA thresholds on the LeetCode Elo scale (spec §5.5; L1 has no floor).
DSA_LADDER = RealmLadder((-math.inf, 1200, 1400, 1600, 1900, 2100, 2400, 2600, 3000))


@dataclass(frozen=True)
class RealmState:
    """One domain's persisted realm position.

    Attributes:
        realm: Current realm (1-based).
        peak_realm: Highest realm ever attained; re-entering at or below it needs no trial.
        unstable_since: First day power was below ``T[realm]``, or None while stable.
        provisional: True when the realm was seeded from history rather than earned by a
            trial (the first run has no trial record — spec §4.9 calls these provisional).
        promoted_at: Day of the last promotion; the Anki trial samples half its words from
            those first reviewed since then.
    """

    realm: int = 1
    peak_realm: int = 1
    unstable_since: Optional[date] = None
    provisional: bool = False
    promoted_at: Optional[date] = None


@dataclass(frozen=True)
class RealmView:
    """What the state machine says about today, for the status card."""

    state: RealmState
    status: str
    next_threshold: Optional[float]
    progress: float
    days_until_demotion: Optional[int] = None
    cooldown_until: Optional[date] = None


class RealmMachine:
    """Runs the §2 state machine for one ladder.

    Args:
        ladder: The domain's thresholds.
        demotion_buffer_days: Days below ``T[L]`` before the realm drops.
    """

    def __init__(self, ladder: RealmLadder, demotion_buffer_days: int) -> None:
        self.ladder = ladder
        self.buffer = demotion_buffer_days

    def seed(self, power: float, today: date) -> RealmState:
        """First run: the realm the power already holds, marked provisional."""
        realm = self.ladder.held_by(power)
        return RealmState(realm=realm, peak_realm=realm, provisional=realm > 1)

    def step(
        self,
        state: RealmState,
        power: float,
        today: date,
        *,
        cooldown_until: Optional[date] = None,
    ) -> RealmView:
        """Apply promotion-by-re-entry and demotion for ``today``; report the status.

        Idempotent within a day, so it may run on every refresh.
        """
        # Re-entry: climb every realm already attained once that the power now holds.
        while (
            state.realm < MAX_REALM
            and state.realm + 1 <= state.peak_realm
            and power >= self.ladder.threshold(state.realm + 1)
        ):
            state = replace(state, realm=state.realm + 1, unstable_since=None)

        days_left: Optional[int] = None
        if power < self.ladder.threshold(state.realm):
            since = state.unstable_since or today
            if (today - since).days >= self.buffer:
                state = replace(
                    state, realm=self.ladder.held_by(power), unstable_since=None
                )
            else:
                state = replace(state, unstable_since=since)
                days_left = self.buffer - (today - since).days
        elif state.unstable_since is not None:
            state = replace(state, unstable_since=None)

        if state.realm >= MAX_REALM:
            status, nxt = PEAK, None
        else:
            nxt = self.ladder.threshold(state.realm + 1)
            if state.unstable_since is not None:
                status = UNSTABLE
            elif power >= nxt:
                cooling = cooldown_until is not None and today < cooldown_until
                status = COOLDOWN if cooling else ELIGIBLE
            else:
                status = STABLE
        return RealmView(
            state=state,
            status=status,
            next_threshold=nxt,
            progress=self.ladder.progress(state.realm, power),
            days_until_demotion=days_left,
            cooldown_until=cooldown_until if status == COOLDOWN else None,
        )

    def pass_trial(self, state: RealmState, power: float, today: date) -> RealmState:
        """A passed trial enters the next realm — only if the power still reaches it."""
        if state.realm >= MAX_REALM or power < self.ladder.threshold(state.realm + 1):
            return state
        realm = state.realm + 1
        return replace(
            state,
            realm=realm,
            peak_realm=max(state.peak_realm, realm),
            unstable_since=None,
            provisional=False,
            promoted_at=today,
        )


def realm_name(realm: int) -> str:
    """The Vietnamese realm name for a 1-based realm."""
    return REALM_NAMES[max(1, min(MAX_REALM, realm)) - 1]
