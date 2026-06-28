"""Pure overdue-detection logic (no Anki imports — unit-testable).

A card is "overdue" when it is both sufficiently late in absolute days and late relative to
its interval. An overdue card is forced to Hard (or Again, if even the Hard interval would
push it too far out).
"""

from __future__ import annotations

from typing import Optional

EASE_AGAIN = 1
EASE_HARD = 2


class OverdueRule:
    """Decides the forced ease for an overdue card."""

    def __init__(
        self, ratio: float, min_days: int, force_again_after_days: int
    ) -> None:
        """Initialise the rule.

        Args:
            ratio: lateness / interval at/above which a card counts as overdue.
            min_days: minimum absolute days late before the rule can trigger.
            force_again_after_days: if the predicted Hard interval exceeds this many days,
                force Again instead of Hard. 0 disables this escalation.
        """
        self._ratio = ratio
        self._min_days = min_days
        self._force_again_after_days = force_again_after_days

    def is_overdue(self, ivl_days: float, late_days: float) -> bool:
        """Return whether a card with these values counts as overdue."""
        if ivl_days <= 0 or late_days < self._min_days:
            return False
        return (late_days / ivl_days) >= self._ratio

    def forced_ease(
        self,
        requested: int,
        ivl_days: float,
        late_days: float,
        hard_ivl_days: float,
    ) -> Optional[int]:
        """Return the ease an overdue card should get, or None to leave ``requested`` as-is.

        Args:
            requested: the ease decided so far (1-4).
            ivl_days: the card's current interval, in days.
            late_days: how many days late the review is.
            hard_ivl_days: the interval (days) a Hard press would schedule.
        """
        if requested == EASE_AGAIN:
            return None  # respect an explicit Again — don't upgrade it
        if not self.is_overdue(ivl_days, late_days):
            return None
        if (
            self._force_again_after_days > 0
            and hard_ivl_days > self._force_again_after_days
        ):
            return EASE_AGAIN
        return EASE_HARD
