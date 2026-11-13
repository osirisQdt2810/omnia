"""Day streaks with one forgiven miss per rolling week. No Anki anywhere."""

from __future__ import annotations

from datetime import date, timedelta

from omnia.plugins.cultivation.streak import compute_streak

D0 = date(2026, 11, 4)


class TestStreak:
    def days(self, *offsets):
        return {D0 - timedelta(days=o) for o in offsets}

    def test_consecutive_days(self):
        s = compute_streak(self.days(0, 1, 2), D0)
        assert (s.days, s.freeze_ready, s.active_today) == (3, True, True)

    def test_today_not_yet_active_does_not_break_it(self):
        assert compute_streak(self.days(1, 2), D0).days == 2

    def test_one_missed_day_is_frozen(self):
        s = compute_streak(self.days(0, 2, 3), D0)
        assert s.days == 3 and s.freeze_ready is False

    def test_two_misses_within_a_week_break_it(self):
        assert compute_streak(self.days(0, 2, 4, 5), D0).days == 2

    def test_misses_a_week_apart_are_both_frozen(self):
        s = compute_streak(
            self.days(0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 17), D0
        )
        # Both misses (8 and 16 days ago) are older than a week, so the freeze is ready again.
        assert s.days == 16 and s.freeze_ready is True

    def test_a_long_break_spends_no_freeze(self):
        s = compute_streak(self.days(30, 31), D0)
        assert (s.days, s.freeze_ready) == (0, True)

    def test_yesterday_missed_is_bridged_when_the_day_before_counts(self):
        s = compute_streak(self.days(2, 3), D0)
        assert (s.days, s.freeze_ready) == (2, False)

    def test_no_activity(self):
        assert compute_streak(set(), D0).days == 0
