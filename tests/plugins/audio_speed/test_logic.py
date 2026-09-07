"""The speed rule: bounds, stepping, rounding, formatting. No Anki anywhere."""

from __future__ import annotations

import pytest

from omnia.plugins.audio_speed.config import AudioSpeedSettings
from omnia.plugins.audio_speed.logic import (
    ANSWER,
    BOTH,
    NORMAL_RATE,
    QUESTION,
    SideSpeeds,
    SpeedBounds,
    SpeedController,
    describe,
    format_rate,
)


def _bounds(minimum=0.5, maximum=3.0, step=0.1) -> SpeedBounds:
    return SpeedBounds(minimum=minimum, maximum=maximum, step=step)


class TestBounds:
    def test_a_rate_inside_the_window_is_returned_rounded(self):
        assert _bounds().clamp(1.2345) == 1.23

    def test_a_rate_below_the_window_is_lifted_to_the_minimum(self):
        assert _bounds(minimum=0.5).clamp(0.1) == 0.5

    def test_a_rate_above_the_window_is_pulled_to_the_maximum(self):
        assert _bounds(maximum=3.0).clamp(9.0) == 3.0

    def test_a_non_positive_step_is_rejected_on_construction(self):
        """A zero step would make every press a no-op that LOOKS like a stuck shortcut."""
        with pytest.raises(ValueError):
            _bounds(step=0)
        with pytest.raises(ValueError):
            _bounds(step=-0.1)

    def test_minimum_above_maximum_is_rejected_on_construction(self):
        with pytest.raises(ValueError):
            _bounds(minimum=2.0, maximum=1.0)

    def test_minimum_equal_to_maximum_is_a_legal_pinned_window(self):
        b = _bounds(minimum=1.5, maximum=1.5)
        assert b.clamp(0.1) == 1.5 and b.clamp(4.0) == 1.5


class TestStepping:
    def test_up_adds_one_step(self):
        c = SpeedController(1.0, _bounds(step=0.25))
        assert c.up() == 1.25
        assert c.rate == 1.25

    def test_down_subtracts_one_step(self):
        c = SpeedController(1.0, _bounds(step=0.25))
        assert c.down() == 0.75

    def test_ten_steps_of_a_tenth_land_exactly_on_two(self):
        """Float drift is the visible bug: 1.9999999999999998× in a tooltip."""
        c = SpeedController(1.0, _bounds(step=0.1))
        for _ in range(10):
            c.up()
        assert c.rate == 2.0

    def test_up_stops_at_the_maximum_and_stays_there(self):
        c = SpeedController(2.9, _bounds(maximum=3.0, step=0.5))
        assert c.up() == 3.0
        assert c.up() == 3.0
        assert c.at_maximum()

    def test_down_stops_at_the_minimum_and_stays_there(self):
        c = SpeedController(0.6, _bounds(minimum=0.5, step=0.5))
        assert c.down() == 0.5
        assert c.down() == 0.5
        assert c.at_minimum()

    def test_reset_returns_to_normal_speed(self):
        c = SpeedController(2.3, _bounds())
        assert c.reset() == NORMAL_RATE

    def test_reset_is_clamped_when_the_window_excludes_normal_speed(self):
        """'Reset' into a 1.5–3.0 window must not escape the window the user set."""
        c = SpeedController(2.5, _bounds(minimum=1.5, maximum=3.0))
        assert c.reset() == 1.5

    def test_the_starting_rate_is_clamped_too(self):
        """A remembered rate from a session with wider bounds must not start out of range."""
        c = SpeedController(4.0, _bounds(maximum=3.0))
        assert c.rate == 3.0

    def test_set_is_the_only_writer_and_it_clamps(self):
        c = SpeedController(1.0, _bounds(minimum=0.5, maximum=3.0))
        assert c.set(7.0) == 3.0
        assert c.set(0.01) == 0.5
        assert c.set(1.234) == 1.23


class TestFormatting:
    @pytest.mark.parametrize(
        "rate,text",
        [
            (1.0, "1×"),
            (1.5, "1.5×"),
            (2.0, "2×"),
            (0.75, "0.75×"),
            (1.25, "1.25×"),
            (0.5, "0.5×"),
        ],
    )
    def test_shortest_exact_form(self, rate, text):
        assert format_rate(rate) == text

    def test_never_shows_float_noise(self):
        assert format_rate(1.9999999999999998) == "2×"


class TestSettingsBounds:
    """A crossed min/max pair must stay READABLE — see the comment on ``max_rate``."""

    def test_a_crossed_pair_still_parses(self):
        # The generic settings form writes the spinboxes without parsing them back through the
        # model, so a rejected pair would reach the config file anyway and then make the whole
        # section raise on read — which disables the plugin AND stops its panel from opening.
        settings = AudioSpeedSettings(min_rate=2.0, max_rate=1.0)
        assert (settings.min_rate, settings.max_rate) == (2.0, 1.0)

    def test_the_plugin_sorts_a_crossed_pair_into_the_window_meant(self):
        bounds = SpeedBounds(*sorted((2.0, 1.0)), step=0.1)
        assert (bounds.minimum, bounds.maximum) == (1.0, 2.0)

    def test_the_default_pair_is_valid(self):
        settings = AudioSpeedSettings()
        assert settings.min_rate <= settings.max_rate


def _speeds(question=1.0, answer=1.0, step=0.1, minimum=0.5, maximum=3.0):
    bounds = SpeedBounds(minimum=minimum, maximum=maximum, step=step)
    return SideSpeeds(
        SpeedController(question, bounds), SpeedController(answer, bounds)
    )


class TestSideSpeeds:
    def test_both_moves_the_two_sides_by_a_step_each(self):
        speeds = _speeds(1.0, 1.0, step=0.25)
        speeds.up(BOTH)
        assert speeds.rates() == {QUESTION: 1.25, ANSWER: 1.25}

    def test_both_preserves_a_gap_instead_of_levelling_it(self):
        # The whole point of two rates: a user who set the back side faster meant that gap, and
        # the plain shortcut must not quietly collapse it into one shared value.
        speeds = _speeds(1.0, 2.0, step=0.5)
        speeds.up(BOTH)
        assert speeds.rates() == {QUESTION: 1.5, ANSWER: 2.5}

    def test_a_side_target_moves_only_that_side(self):
        speeds = _speeds(1.0, 1.0, step=0.5)
        speeds.up(QUESTION)
        assert speeds.rates() == {QUESTION: 1.5, ANSWER: 1.0}
        speeds.down(ANSWER)
        assert speeds.rates() == {QUESTION: 1.5, ANSWER: 0.5}

    def test_each_side_stops_at_its_own_bound(self):
        speeds = _speeds(2.9, 1.0, step=0.5, maximum=3.0)
        speeds.up(BOTH)
        assert speeds.rates() == {QUESTION: 3.0, ANSWER: 1.5}

    def test_reset_returns_both_to_normal(self):
        speeds = _speeds(2.0, 0.6)
        speeds.reset(BOTH)
        assert speeds.rates() == {QUESTION: 1.0, ANSWER: 1.0}

    def test_reset_can_target_one_side(self):
        speeds = _speeds(2.0, 0.6)
        speeds.reset(ANSWER)
        assert speeds.rates() == {QUESTION: 2.0, ANSWER: 1.0}

    def test_matched_reports_whether_the_sides_agree(self):
        assert _speeds(1.5, 1.5).matched()
        assert not _speeds(1.5, 1.0).matched()

    def test_an_unknown_side_answers_as_the_question_side(self):
        # The callers are a render hook and a menu action; a wrong name should land on the side
        # the user is most likely looking at, not raise into the middle of a card.
        speeds = _speeds(1.4, 2.0)
        assert speeds.rate_for("sideways") == 1.4


class TestDescribe:
    def test_both_sides_equal_reads_as_one_speed(self):
        assert describe(_speeds(1.5, 1.5), BOTH) == "Audio speed 1.5×"

    def test_both_sides_differing_names_each_one(self):
        assert describe(_speeds(1.5, 2.0), BOTH) == "Audio speed — front 1.5×, back 2×"

    def test_a_single_side_is_named(self):
        # A press can change a rate the user cannot hear yet — bumping the back side while the
        # question is on screen does nothing audible until the flip — so the tooltip has to say
        # which side moved or it looks like the key did nothing.
        assert describe(_speeds(1.5, 1.0), QUESTION) == "Audio speed 1.5× (front)"
        assert describe(_speeds(1.5, 2.0), ANSWER) == "Audio speed 2× (back)"
