"""Tests for the SQLite typed-answer log + the session tracker (pure, headless sqlite)."""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from omnia.plugins.typed_accuracy import store as store_mod
from omnia.plugins.typed_accuracy.store import (
    RESULT_BAD,
    RESULT_EMPTY,
    RESULT_GOOD,
    RESULT_MISS,
    SessionTracker,
    TypedAnswerLog,
)


class _DbAdapter:
    """Expose a ``sqlite3`` connection through Anki's ``.execute`` / ``.scalar`` surface."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        return self._conn.execute(sql, args)

    def scalar(self, sql: str, *args: object):
        row = self._conn.execute(sql, args).fetchone()
        return row[0] if row else None


_DECKS = [(1, "Spanish"), (2, "Spanish::Verbs"), (3, "French")]


@pytest.fixture
def log(monkeypatch):
    """A TypedAnswerLog over in-memory sqlite, with a monotonic clock and a deck tree."""
    conn = sqlite3.connect(":memory:")
    clock = itertools.count(1000, step=10)  # deterministic, increasing ts_ms
    monkeypatch.setattr(store_mod, "now_ms", lambda: next(clock))
    return TypedAnswerLog(_DbAdapter(conn), deck_provider=lambda: list(_DECKS))


class TestTypedAnswerLog:
    def test_insert_and_attempts_counts(self, log):
        log.insert_log(cid=1, did=1, card_did=1, result=RESULT_GOOD)
        log.insert_log(cid=2, did=1, card_did=1, result=RESULT_BAD)
        log.insert_log(cid=3, did=1, card_did=1, result=RESULT_MISS)
        log.insert_log(cid=4, did=1, card_did=1, result=RESULT_EMPTY)

        stats = log.query_stats(1, include_subdecks=False, start_ms=0, end_ms=10**18)
        a = stats["attempts"]
        assert a["total"] == 4
        assert (a["good"], a["bad"], a["miss"], a["empty"]) == (1, 1, 1, 1)
        assert a["p_good"] == pytest.approx(25.0)

    def test_unique_last_collapses_to_latest_per_card(self, log):
        # Same card answered twice: bad then good. attempts=2, unique_last=1 (good).
        log.insert_log(cid=1, did=1, card_did=1, result=RESULT_BAD)
        log.insert_log(cid=1, did=1, card_did=1, result=RESULT_GOOD)

        stats = log.query_stats(1, include_subdecks=False, start_ms=0, end_ms=10**18)
        assert stats["attempts"]["total"] == 2
        assert stats["attempts"]["bad"] == 1
        assert stats["attempts"]["good"] == 1

        u = stats["unique_last"]
        assert u["total"] == 1
        assert u["good"] == 1
        assert u["bad"] == 0
        assert u["p_good"] == pytest.approx(100.0)

    def test_subdeck_rollup_includes_descendants_by_prefix(self, log):
        log.insert_log(cid=1, did=1, card_did=1, result=RESULT_GOOD)  # Spanish
        log.insert_log(cid=2, did=2, card_did=2, result=RESULT_GOOD)  # Spanish::Verbs
        log.insert_log(cid=3, did=3, card_did=3, result=RESULT_GOOD)  # French (sibling)

        # Without subdecks: only deck 1's row.
        only = log.query_stats(1, include_subdecks=False, start_ms=0, end_ms=10**18)
        assert only["attempts"]["total"] == 1

        # With subdecks: deck 1 + its "Spanish::" descendant (deck 2), not French.
        rolled = log.query_stats(1, include_subdecks=True, start_ms=0, end_ms=10**18)
        assert rolled["attempts"]["total"] == 2

    def test_time_window_filters_rows(self, log):
        # Clock starts at 1000 and steps by 10: rows land at 1000, 1010, 1020.
        log.insert_log(cid=1, did=1, card_did=1, result=RESULT_GOOD)  # ts 1000
        log.insert_log(cid=2, did=1, card_did=1, result=RESULT_GOOD)  # ts 1010
        log.insert_log(cid=3, did=1, card_did=1, result=RESULT_GOOD)  # ts 1020

        # Window [1005, 1020): excludes the first (1000) and the last (1020, end-exclusive).
        windowed = log.query_stats(
            1, include_subdecks=False, start_ms=1005, end_ms=1020
        )
        assert windowed["attempts"]["total"] == 1

    def test_empty_query_is_all_zeros(self, log):
        stats = log.query_stats(1, include_subdecks=False, start_ms=0, end_ms=10**18)
        assert stats["attempts"]["total"] == 0
        assert stats["attempts"]["p_good"] == 0.0  # no division by zero
        assert stats["unique_last"]["total"] == 0

    def test_descendants_without_provider_returns_self_only(self):
        conn = sqlite3.connect(":memory:")
        bare = TypedAnswerLog(_DbAdapter(conn))  # no deck provider
        assert bare.deck_descendants_by_prefix(5) == [5]


class TestSessionTracker:
    def test_mark_and_get_open_ms(self):
        clock = iter([111, 222])
        tracker = SessionTracker(now=lambda: next(clock))
        assert tracker.get_open_ms(7) == 0  # not entered yet
        tracker.mark_review_entered(7)
        assert tracker.get_open_ms(7) == 111
        # A second entry for the same deck keeps the first open time.
        tracker.mark_review_entered(7)
        assert tracker.get_open_ms(7) == 111

    def test_clear_forgets_sessions(self):
        tracker = SessionTracker(now=lambda: 500)
        tracker.mark_review_entered(7)
        tracker.clear()
        assert tracker.get_open_ms(7) == 0
