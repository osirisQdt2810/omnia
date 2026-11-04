"""The realm ladder and state machine (spec §2). No Anki anywhere."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from omnia.plugins.cultivation.realms import (
    ANKI_LADDER,
    COOLDOWN,
    DSA_LADDER,
    ELIGIBLE,
    PEAK,
    STABLE,
    UNSTABLE,
    RealmLadder,
    RealmMachine,
    RealmState,
    realm_name,
)

D0 = date(2026, 11, 4)


def machine(buffer: int = 14) -> RealmMachine:
    return RealmMachine(ANKI_LADDER, buffer)


class TestLadder:
    def test_held_by_finds_the_highest_threshold_reached(self):
        assert ANKI_LADDER.held_by(0) == 1
        assert ANKI_LADDER.held_by(999.9) == 1
        assert ANKI_LADDER.held_by(1000) == 2
        assert ANKI_LADDER.held_by(2431) == 3
        assert ANKI_LADDER.held_by(99999) == 9

    def test_dsa_l1_has_no_floor(self):
        assert math.isinf(DSA_LADDER.threshold(1))
        assert DSA_LADDER.held_by(-500) == 1
        assert DSA_LADDER.held_by(1500) == 3

    def test_progress_is_the_share_of_the_way_to_the_next_realm(self):
        # Spec mock: 2,431 in Kim Đan (2,000 → 3,000) is 43% of this realm.
        assert ANKI_LADDER.progress(3, 2431) == pytest.approx(0.431)
        assert ANKI_LADDER.progress(9, 20000) == 1.0

    def test_a_ladder_must_increase(self):
        with pytest.raises(ValueError):
            RealmLadder((0, 1, 1, 2, 3, 4, 5, 6, 7))

    def test_names_follow_the_spec(self):
        assert realm_name(1) == "Luyện Khí"
        assert realm_name(3) == "Kim Đan"
        assert realm_name(9) == "Chân Tiên"


class TestPromotion:
    def test_reaching_a_new_realm_only_makes_it_eligible(self):
        view = machine().step(RealmState(realm=2, peak_realm=2), 2100, D0)
        assert view.state.realm == 2
        assert view.status == ELIGIBLE

    def test_a_realm_held_before_is_reentered_without_a_trial(self):
        view = machine().step(RealmState(realm=2, peak_realm=4), 3100, D0)
        assert view.state.realm == 4
        assert view.status == STABLE

    def test_reentry_stops_at_the_peak(self):
        view = machine().step(RealmState(realm=1, peak_realm=3), 9000, D0)
        assert view.state.realm == 3
        assert view.status == ELIGIBLE

    def test_a_passed_trial_enters_the_next_realm_and_raises_the_peak(self):
        state = machine().pass_trial(
            RealmState(realm=2, peak_realm=2, provisional=True), 2100, D0
        )
        assert (state.realm, state.peak_realm, state.promoted_at) == (3, 3, D0)
        assert state.provisional is False

    def test_a_passed_trial_needs_the_power_at_that_moment(self):
        state = RealmState(realm=2, peak_realm=2)
        assert machine().pass_trial(state, 1999, D0) == state

    def test_cooldown_replaces_eligible_until_it_ends(self):
        state = RealmState(realm=2, peak_realm=2)
        until = D0 + timedelta(days=3)
        assert machine().step(state, 2100, D0, cooldown_until=until).status == COOLDOWN
        assert (
            machine().step(state, 2100, until, cooldown_until=until).status == ELIGIBLE
        )

    def test_the_top_realm_is_peak(self):
        assert machine().step(RealmState(9, 9), 20000, D0).status == PEAK


class TestDemotion:
    def test_falling_below_marks_unstable_but_keeps_the_realm(self):
        view = machine().step(RealmState(realm=3, peak_realm=3), 1990, D0)
        assert view.state.realm == 3
        assert view.status == UNSTABLE
        assert view.state.unstable_since == D0
        assert view.days_until_demotion == 14

    def test_the_realm_drops_after_fourteen_days_below(self):
        state = RealmState(realm=3, peak_realm=3, unstable_since=D0)
        assert machine().step(state, 1990, D0 + timedelta(days=13)).state.realm == 3
        dropped = machine().step(state, 1990, D0 + timedelta(days=14)).state
        assert dropped.realm == 2
        assert dropped.unstable_since is None
        assert dropped.peak_realm == 3

    def test_recovering_clears_unstable(self):
        state = RealmState(realm=3, peak_realm=3, unstable_since=D0)
        view = machine().step(state, 2005, D0 + timedelta(days=10))
        assert view.state.unstable_since is None
        assert view.status == STABLE

    def test_after_a_drop_the_lost_realm_comes_back_without_a_trial(self):
        state = RealmState(realm=2, peak_realm=3)
        assert machine().step(state, 2001, D0).state.realm == 3

    def test_step_is_idempotent_within_a_day(self):
        m = machine()
        first = m.step(RealmState(realm=3, peak_realm=3), 1990, D0)
        again = m.step(first.state, 1990, D0)
        assert again.state == first.state


def test_seed_is_provisional_above_l1():
    assert machine().seed(2431, D0) == RealmState(
        realm=3, peak_realm=3, provisional=True
    )
    assert machine().seed(10, D0).provisional is False
