"""Tests for how far along a pull is, and how much longer it has.

A pull carries media, so a few chosen decks can be hundreds of megabytes and several minutes. Two
things follow, and both are properties of this module rather than of any screen:

* the user can put the window away and keep studying, so the same numbers have to render in three
  places — the picker's bar, the Sync button filling behind its label, and that button's tooltip;
* "it is working" is not good enough. They want a percentage and a time, and a percentage that
  lies is worse than none.

The clock is injected, so nothing here is timing-dependent.
"""

from __future__ import annotations

import pytest

from omnia.core.sync.progress import (
    DONE,
    DOWNLOADING,
    FAILED,
    IMPORTING,
    PREPARING,
    Progress,
    ProgressTracker,
    format_bytes,
    format_duration,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def tracker(clock) -> ProgressTracker:
    return ProgressTracker(clock)


class TestWhatItRefusesToGuess:
    def test_there_is_no_percentage_before_a_size_is_known(self, tracker):
        # None, not 0. A bar pinned at zero for forty seconds while a collection is packed reads
        # as stuck; the caller shows a moving indeterminate bar instead.
        assert tracker.snapshot().fraction is None
        assert tracker.snapshot().percent is None

    def test_there_is_no_estimate_from_a_single_sample(self, tracker, clock):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        tracker.advance(100)
        clock.tick(1.0)
        tracker.advance(200)

        assert tracker.snapshot().eta_seconds is None

    def test_a_stall_produces_no_estimate_rather_than_infinity(self, tracker, clock):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        for _ in range(6):
            clock.tick(1.0)
            tracker.advance(100)  # the same byte count every time: nothing is moving

        assert tracker.snapshot().eta_seconds is None

    def test_a_total_of_zero_is_treated_as_unknown(self, tracker):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(0)

        assert tracker.snapshot().fraction is None


class TestTheNumbersItDoesGive:
    def _download(self, tracker, clock, *, total=1000, chunk=100, times=5):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(total)
        for step in range(1, times + 1):
            clock.tick(1.0)
            tracker.advance(chunk * step)

    def test_the_percentage_follows_the_bytes(self, tracker, clock):
        self._download(tracker, clock, total=1000, chunk=100, times=3)

        assert tracker.snapshot().percent == 30

    def test_the_percentage_is_floored_never_rounded_up(self, tracker, clock):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        tracker.advance(997)

        # 99.7% shown as 100% beside a bar that is still moving is the one number a reader calls
        # a lie.
        assert tracker.snapshot().percent == 99

    def test_the_estimate_uses_the_recent_rate(self, tracker, clock):
        # 100 bytes a second, 500 of 1000 done -> about five seconds left.
        self._download(tracker, clock, total=1000, chunk=100, times=5)

        eta = tracker.snapshot().eta_seconds
        assert eta is not None and 4.0 <= eta <= 6.0

    def test_it_follows_a_change_of_speed_rather_than_averaging_the_whole_run(
        self, tracker, clock
    ):
        # The two phases of a pull run at different speeds, and an average would spend most of a
        # fast download quoting the slow export's pace.
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(10_000)
        done = 0
        for _ in range(6):  # slow: 100/s
            clock.tick(1.0)
            done += 100
            tracker.advance(done)
        slow = tracker.snapshot().eta_seconds
        for _ in range(12):  # fast: 1000/s
            clock.tick(1.0)
            done += 1000
            tracker.advance(done)
        fast = tracker.snapshot().eta_seconds

        assert slow is not None and fast is not None
        assert fast < slow / 5

    def test_finishing_is_one_hundred_percent_even_without_a_total(self, tracker):
        tracker.set_phase(DONE)

        assert tracker.snapshot().fraction == 1.0
        assert tracker.snapshot().percent == 100

    def test_progress_past_the_total_does_not_exceed_one(self, tracker):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(100)
        tracker.advance(150)

        assert tracker.snapshot().fraction == 1.0

    def test_bytes_never_go_backwards(self, tracker):
        # Chunks can be re-reported; a bar that jumps back reads as a restart.
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        tracker.advance(500)
        tracker.advance(400)

        assert tracker.snapshot().done_bytes == 500


class TestMovingBetweenPhases:
    def test_a_new_phase_starts_its_counting_over(self, tracker, clock):
        # The phases count different things: bytes packed, then bytes copied. Carrying one into
        # the next would show a download starting at 100%.
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        tracker.advance(1000)

        tracker.set_phase(IMPORTING)

        assert tracker.snapshot().done_bytes == 0
        assert tracker.snapshot().fraction is None

    def test_the_estimate_does_not_survive_a_phase_change(self, tracker, clock):
        tracker.set_phase(DOWNLOADING)
        tracker.set_total(1000)
        for step in range(1, 6):
            clock.tick(1.0)
            tracker.advance(100 * step)
        assert tracker.snapshot().eta_seconds is not None

        tracker.set_phase(IMPORTING)

        assert tracker.snapshot().eta_seconds is None


class TestWhatTheUserReads:
    def test_each_phase_says_which_machine_is_working(self, tracker):
        # Written from the target's point of view — this is the machine they are sitting at.
        assert "other computer" in Progress(phase=PREPARING).label
        assert "this collection" in Progress(phase=IMPORTING).label

    def test_the_tooltip_carries_the_percentage_and_the_time(self):
        line = Progress(
            phase=DOWNLOADING, done_bytes=250, total_bytes=1000, eta_seconds=95
        ).summary()

        assert "25%" in line
        assert "2 minutes left" in line

    def test_a_finished_pull_does_not_offer_an_estimate(self):
        line = Progress(
            phase=DONE, done_bytes=10, total_bytes=10, eta_seconds=3
        ).summary()

        assert "left" not in line
        assert line == "Done."

    def test_a_failed_pull_says_stopped_and_carries_the_reason(self):
        line = Progress(phase=FAILED, detail="The other computer went away.").summary()

        assert line.startswith("Stopped.")
        assert "went away" in line


class TestHowLongItSays:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "a few seconds"),
            (7, "a few seconds"),
            (12, "10 seconds"),
            (47, "45 seconds"),
            (95, "2 minutes"),
            (60 * 5, "5 minutes"),
            (60 * 60, "1 hour"),
            (60 * 65, "1 hour 5 minutes"),
            (60 * 121, "2 hours 1 minute"),
        ],
    )
    def test_it_is_rounded_coarsely(self, seconds, expected):
        # An estimate to the second implies a precision it does not have, and a number that ticks
        # 47, 52, 45 reads as broken rather than as honest.
        assert format_duration(seconds) == expected

    def test_a_negative_estimate_is_not_shown_as_negative(self):
        assert format_duration(-5) == "a few seconds"


class TestHowBigItSays:
    @pytest.mark.parametrize(
        "count,expected",
        [
            (0, ""),
            (None, ""),
            (512, "512 bytes"),
            (2_500, "2.5 KB"),
            (742_000_000, "742 MB"),
        ],
    )
    def test_sizes_read_the_way_a_download_is_quoted(self, count, expected):
        assert format_bytes(count) == expected
