"""Trials and streaks (spec §4.6, §5.6, §2). No Anki anywhere."""

from __future__ import annotations

from datetime import date, timedelta
from math import comb

import pytest

from omnia.plugins.cultivation.config import PASSIVE_RECALL, AnkiTuning, DsaTuning
from omnia.plugins.cultivation.dsa import Attempt, Problem
from omnia.plugins.cultivation.memory import CardMemory
from omnia.plugins.cultivation.streak import compute_streak
from omnia.plugins.cultivation.trials import (
    PASSIVE,
    TYPED,
    AnkiTrial,
    DsaTrial,
    cid_search,
    damerau_levenshtein,
)
from omnia.plugins.cultivation.vocab import VocabCard, group_words

NOW = 1_800_000_000.0
D0 = date(2026, 11, 4)


def words(n, typed_every=1, hours_ago=48, first_ms=1):
    cards = [
        VocabCard(
            i,
            f"w{i:03}",
            PASSIVE_RECALL,
            CardMemory(10, 0.5, NOW - hours_ago * 3600),
            typed=(i % typed_every == 0),
            meaning=f"m{i}",
            first_review_ms=first_ms * i,
        )
        for i in range(1, n + 1)
    ]
    return group_words(cards)


class TestAnkiTrial:
    def test_the_pass_mark_has_the_spec_operating_characteristic(self):
        """43/50: P(pass) ≈ 0.19 / 0.52 / 0.88 at true recall 0.80 / 0.85 / 0.90."""

        def p_pass(p):
            return sum(comb(50, k) * p**k * (1 - p) ** (50 - k) for k in range(43, 51))

        assert p_pass(0.80) == pytest.approx(0.19, abs=0.01)
        assert p_pass(0.85) == pytest.approx(0.52, abs=0.01)
        assert p_pass(0.90) == pytest.approx(0.88, abs=0.01)
        assert AnkiTrial(AnkiTuning()).passed(43) and not AnkiTrial(
            AnkiTuning()
        ).passed(42)

    def test_recently_reviewed_words_are_excluded(self):
        t = AnkiTrial(AnkiTuning())
        fresh = words(3, hours_ago=2)
        assert t.eligible_words(fresh.values(), NOW) == []
        assert len(t.eligible_words(words(3).values(), NOW)) == 3

    def test_sampling_is_reproducible_from_the_seed(self):
        t = AnkiTrial(AnkiTuning())
        pool = t.eligible_words(words(200).values(), NOW)
        a = t.sample(pool, 1234, since_ms=None)
        b = t.sample(pool, 1234, since_ms=None)
        assert a == b and len(a[1]) == 50
        assert t.sample(pool, 99, since_ms=None) != a

    def test_half_the_sample_comes_from_words_since_the_last_promotion(self):
        t = AnkiTrial(AnkiTuning())
        pool = t.eligible_words(words(200).values(), NOW)
        _mode, items = t.sample(
            pool, 7, since_ms=181
        )  # words 181..200 are "new": only 20
        fresh = [i for i in items if int(i.word[1:]) >= 181]
        assert len(fresh) == 20  # all of them, the rest from the whole pool
        _mode, items = t.sample(pool, 7, since_ms=101)  # 100 new words: exactly half
        assert len([i for i in items if int(i.word[1:]) >= 101]) >= 25

    def test_mode_is_typed_only_when_most_words_have_a_typed_card(self):
        t = AnkiTrial(AnkiTuning())
        assert t.sample(list(words(60).values()), 1, since_ms=None)[0] == TYPED
        assert (
            t.sample(list(words(60, typed_every=3).values()), 1, since_ms=None)[0]
            == PASSIVE
        )

    def test_typed_grading(self):
        t = AnkiTrial(AnkiTuning())
        assert t.grade_typed("Ephemeral", " ephemeral ")
        assert t.grade_typed("ephemeral", "ephemreal")  # transposition, 9 letters
        assert not t.grade_typed("brave", "brvae")  # too short for a typo
        assert not t.grade_typed("ephemeral", "ephmrl")
        assert not t.grade_typed("x", "")

    def test_damerau_levenshtein(self):
        assert damerau_levenshtein("abcd", "abdc") == 1
        assert damerau_levenshtein("kitten", "sitting") == 3

    def test_cid_search(self):
        assert cid_search([3, 1, 3]) == "cid:1,3"
        assert cid_search([]) == ""


def problem(pid, rating, contest):
    return Problem(pid, f"P{pid}", f"p{pid}", rating, contest)


POOL = [
    problem(i, 1600 + (i % 9) * 20 - 80, f"weekly-contest-{i % 4}")
    for i in range(1, 40)
]


class TestDsaTrial:
    def test_picks_unseen_problems_near_the_target_from_two_contests(self):
        t = DsaTrial(DsaTuning())
        picked = t.pick(POOL, seen_ids={1, 2, 3}, target=1600, seed=5)
        assert len(picked) == 5
        assert all(
            abs(p.rating - 1600) <= 100 and p.id not in {1, 2, 3} for p in picked
        )
        assert len({p.contest for p in picked}) >= 2
        assert picked == t.pick(POOL, seen_ids={1, 2, 3}, target=1600, seed=5)

    def test_an_empty_pool_gives_no_trial(self):
        assert DsaTrial(DsaTuning()).pick(POOL, (), target=2900, seed=1) == []

    def test_one_contest_is_not_enough(self):
        same = [problem(i, 1600, "c") for i in range(10)]
        assert DsaTrial(DsaTuning()).pick(same, (), target=1600, seed=1) == []

    def test_only_fast_self_solves_after_the_start_count(self):
        t = DsaTrial(DsaTuning())
        probs = [problem(1, 1500, "a"), problem(2, 1500, "b"), problem(3, 1500, "a")]
        attempts = [
            Attempt(1, D0 + timedelta(days=1), "self", 30),
            Attempt(2, D0 + timedelta(days=1), "hint", 10),
            Attempt(3, D0 - timedelta(days=1), "self", 10),  # before the trial
        ]
        items = t.evaluate(probs, attempts, D0)
        assert [i.solved_on for i in items] == [D0 + timedelta(days=1), None, None]
        assert "hint" in items[1].note

    def test_too_slow_or_untimed_does_not_count(self):
        t = DsaTrial(DsaTuning())
        probs = [problem(1, 1950, "a"), problem(2, 1300, "b")]
        attempts = [Attempt(1, D0, "self", 61), Attempt(2, D0, "self", None)]
        items = t.evaluate(probs, attempts, D0)
        assert [i.solved_on for i in items] == [None, None]
        assert items[1].note == "needs @time"

    def test_verdict(self):
        t = DsaTrial(DsaTuning())
        probs = [problem(i, 1500, "a") for i in range(5)]
        three = [Attempt(i, D0 + timedelta(days=2), "self", 10) for i in range(3)]
        assert (
            t.verdict(t.evaluate(probs, three, D0), D0, D0 + timedelta(days=2)) is True
        )
        two = three[:2]
        assert (
            t.verdict(t.evaluate(probs, two, D0), D0, D0 + timedelta(days=14)) is None
        )
        assert (
            t.verdict(t.evaluate(probs, two, D0), D0, D0 + timedelta(days=15)) is False
        )


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
