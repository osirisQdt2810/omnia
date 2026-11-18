"""The DSA feed and Elo replay (spec §5.2–§5.4). No Anki anywhere."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from omnia.plugins.cultivation.config import DsaTuning
from omnia.plugins.cultivation.dsa import (
    Attempt,
    DsaFeed,
    EloEngine,
    FeedError,
    RatingBook,
    expected,
    time_limit,
)

D0 = date(2026, 1, 1)
T = DsaTuning()


def feed(attempts=(), problems=(), difficulty=None) -> DsaFeed:
    return DsaFeed.parse(
        {
            "schema": 1,
            "generated_at": "2026-11-04T10:00:00+07:00",
            "attempts": list(attempts),
            "problems": list(problems),
            "difficulty": difficulty or {},
        }
    )


def row(lc_id, day, status="self", minutes=10, wa=0, path=None):
    return {
        "lc_id": lc_id,
        "date": day.isoformat(),
        "status": status,
        "minutes": minutes,
        "wa": wa,
        "path": path or f"src/{lc_id}-{day}.cpp",
        "source": "daily",
    }


def prob(pid, rating, contest="weekly-contest-1"):
    return {
        "id": pid,
        "title": f"P{pid}",
        "slug": f"p{pid}",
        "rating": rating,
        "contest": contest,
    }


def engine(f: DsaFeed, **tuning) -> EloEngine:
    t = DsaTuning(**tuning)
    return EloEngine(t, RatingBook(f, t))


class TestFeed:
    def test_rows_are_parsed_and_sorted_by_date(self):
        f = feed([row(2, D0 + timedelta(days=1)), row(1, D0)])
        assert [a.lc_id for a in f.attempts] == [1, 2]
        assert f.attempts[0] == Attempt(
            1, D0, "self", 10, 0, None, "src/1-2026-01-01.cpp", "daily"
        )

    def test_malformed_rows_are_skipped(self):
        f = feed([{"lc_id": "x"}, row(1, D0), {"date": "2026-01-01"}])
        assert len(f.attempts) == 1

    def test_unknown_status_is_unknown(self):
        assert feed([row(1, D0, status="vibes")]).attempts[0].status == "unknown"

    def test_wrong_schema_is_refused(self):
        with pytest.raises(FeedError):
            DsaFeed.parse({"schema": 2})

    def test_load_returns_none_without_a_file(self, tmp_path):
        assert DsaFeed.load(tmp_path / "missing.json") is None

    def test_load_reads_a_real_file(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text(
            json.dumps({"schema": 1, "attempts": [row(1, D0)]}), encoding="utf-8"
        )
        assert DsaFeed.load(path).attempts[0].lc_id == 1


class TestRatings:
    def test_zerotrac_rating_wins_over_difficulty(self):
        book = RatingBook(feed(problems=[prob(1, 1850.5)], difficulty={"1": "Easy"}), T)
        assert book.rating(1) == 1850.5

    def test_unrated_problems_use_the_difficulty_fallback(self):
        book = RatingBook(feed(difficulty={"1": "Easy", "2": "Medium", "3": "Hard"}), T)
        assert (book.rating(1), book.rating(2), book.rating(3)) == (1200, 1600, 2000)
        assert book.rating(4) is None

    @pytest.mark.parametrize(
        "rating,limit", [(1399, 20), (1400, 40), (1899, 40), (1900, 60)]
    )
    def test_time_limits(self, rating, limit):
        assert time_limit(rating, T) == limit


class TestElo:
    def test_expected_is_half_at_equal_rating(self):
        assert expected(1600, 1600) == 0.5
        assert expected(1600, 2000) == pytest.approx(1 / 11)

    def test_one_fast_self_solve_follows_the_formula(self):
        f = feed([row(1, D0, minutes=10, wa=0)], [prob(1, 1700)])
        out = engine(f).replay(f.attempts, D0)
        assert out.theta == pytest.approx(1500 + 40 * (1.0 - expected(1500, 1700)))

    @pytest.mark.parametrize(
        "status,minutes,wa,s",
        [
            ("self", 10, 0, 1.0),
            ("self", 99, 0, 0.8),
            ("self", 10, 3, 0.8),
            ("self", None, 0, 0.8),
            ("hint", 10, 0, 0.5),
            ("editorial", 10, 0, 0.2),
            ("copied", 10, 0, 0.0),
        ],
    )
    def test_quality_scores(self, status, minutes, wa, s):
        e = engine(feed())
        assert e.quality(Attempt(1, D0, status, minutes, wa), 1500) == (s, 1.0)

    def test_legacy_unknown_counts_at_half_weight(self):
        assert engine(feed()).quality(Attempt(1, D0, "unknown"), 1500) == (0.7, 0.5)

    def test_unrated_problems_do_not_move_theta(self):
        f = feed([row(1, D0)])
        out = engine(f).replay(f.attempts, D0)
        assert out.theta == 1500 and out.scored[0].note == "unrated"

    def test_k_drops_to_twenty_after_thirty_rated_attempts(self):
        rows = [
            row(i, D0 + timedelta(days=i % 5), path=f"{i:03}") for i in range(1, 32)
        ]
        f = feed(rows, [prob(i, 1500) for i in range(1, 32)])
        out = engine(f).replay(f.attempts, D0 + timedelta(days=5))
        ks = [s.k for s in out.scored]
        assert ks[:30] == [40.0] * 30 and ks[30] == 20.0

    def test_k_is_ten_once_theta_ever_reached_2400(self):
        f = feed([row(1, D0)], [prob(1, 2500)])
        out = engine(f, initial_rating=2450).replay(f.attempts, D0)
        assert out.scored[0].k == 10.0

    def test_resolve_within_a_week_is_lifetime_only(self):
        f = feed([row(1, D0), row(1, D0 + timedelta(days=3))], [prob(1, 1700)])
        out = engine(f).replay(f.attempts, D0 + timedelta(days=3))
        assert (
            out.scored[1].k is None
            and out.scored[1].theta_after == out.scored[0].theta_after
        )

    def test_resolve_after_a_week_counts_at_half_weight(self):
        f = feed([row(1, D0), row(1, D0 + timedelta(days=7))], [prob(1, 1700)])
        out = engine(f).replay(f.attempts, D0 + timedelta(days=7))
        assert out.scored[1].w == 0.5

    def test_post_break_boost(self):
        later = D0 + timedelta(days=40)
        rows = [row(i, D0, path=f"a{i:02}") for i in range(1, 32)] + [row(99, later)]
        f = feed(rows, [prob(i, 1500) for i in [*range(1, 32), 99]])
        out = engine(f).replay(f.attempts, later)
        assert out.scored[-1].k == 40.0


class TestDecay:
    def test_no_decay_inside_the_grace_week(self):
        f = feed([row(1, D0)], [prob(1, 1700)])
        e = engine(f)
        assert (
            e.replay(f.attempts, D0 + timedelta(days=7)).theta
            == e.replay(f.attempts, D0).theta
        )

    def test_decay_halves_the_distance_to_the_floor_every_half_life(self):
        f = feed([row(1, D0)], [prob(1, 3000)])
        e = engine(f, initial_rating=1800)
        theta0 = e.replay(f.attempts, D0).theta
        peak = theta0
        floor = 1200 + 0.5 * (peak - 1200)
        after = e.replay(f.attempts, D0 + timedelta(days=7 + 90)).theta
        assert after == pytest.approx(floor + (theta0 - floor) / 2, rel=1e-9)

    def test_decay_never_crosses_the_floor(self):
        f = feed([row(1, D0)], [prob(1, 3000)])
        e = engine(f, initial_rating=1800)
        theta = e.replay(f.attempts, D0 + timedelta(days=5000)).theta
        assert theta >= 1200 + 0.5 * (e.replay(f.attempts, D0).theta_peak - 1200) - 1e-6

    def test_history_has_one_point_per_day(self):
        f = feed([row(1, D0)], [prob(1, 1600)])
        out = engine(f).replay(f.attempts, D0 + timedelta(days=9))
        assert len(out.history) == 10 and out.history[-1][0] == D0 + timedelta(days=9)


class TestStartDay:
    def test_attempts_before_the_start_are_ignored(self):
        f = feed(
            [row(1, D0), row(2, D0 + timedelta(days=5))], [prob(1, 1700), prob(2, 1700)]
        )
        out = engine(f).replay(
            f.attempts, D0 + timedelta(days=5), start=D0 + timedelta(days=3)
        )
        assert [s.attempt.lc_id for s in out.scored] == [2]
        assert out.history[0][0] == D0 + timedelta(days=3)

    def test_no_attempts_since_the_start_keeps_the_initial_rating(self):
        f = feed([row(1, D0)], [prob(1, 1700)])
        out = engine(f).replay(
            f.attempts, D0 + timedelta(days=12), start=D0 + timedelta(days=10)
        )
        assert out.theta == 1500 and len(out.history) == 3 and out.scored == []


class TestDebts:
    def test_copied_opens_a_debt(self):
        f = feed([row(1, D0, status="copied")], [prob(1, 1600)])
        debts = engine(f).replay(f.attempts, D0).debts
        assert [(d.lc_id, d.closed) for d in debts] == [(1, None)]

    def test_a_spaced_self_solve_closes_it(self):
        later = D0 + timedelta(days=8)
        f = feed(
            [row(1, D0, status="copied"), row(1, later, status="self")], [prob(1, 1600)]
        )
        assert engine(f).replay(f.attempts, later).debts[0].closed == later

    def test_a_quick_resolve_does_not_close_it(self):
        later = D0 + timedelta(days=2)
        f = feed(
            [row(1, D0, status="copied"), row(1, later, status="self")], [prob(1, 1600)]
        )
        assert engine(f).replay(f.attempts, later).debts[0].closed is None
