"""Tests for the reviewer ease pipeline (pure fold + the single _answerCard wrap)."""

from __future__ import annotations

from omnia.core.reviewer.ease_pipeline import EasePipeline, _Entry, fold_ease


class TestFoldEase:
    def test_fold_passes_through_when_no_transformers(self):
        assert fold_ease(object(), 3, []) == 3

    def test_fold_applies_in_priority_order(self):
        # earlier (lower priority) sets 4, later (higher priority) caps to 2 -> 2 wins
        entries = sorted(
            [
                _Entry(200, "guard", lambda c, e: 2),
                _Entry(100, "typed", lambda c, e: 4),
            ]
        )
        assert fold_ease(object(), 1, entries) == 2

    def test_fold_skips_none_results(self):
        entries = sorted(
            [
                _Entry(100, "typed", lambda c, e: 4),
                _Entry(200, "guard", lambda c, e: None),  # not overdue -> no change
            ]
        )
        assert fold_ease(object(), 1, entries) == 4

    def test_fold_clamps_out_of_range(self):
        entries = [_Entry(100, "x", lambda c, e: 99)]
        assert fold_ease(object(), 1, entries) == 4
        entries = [_Entry(100, "x", lambda c, e: -5)]
        assert fold_ease(object(), 1, entries) == 1

    def test_fold_clamps_to_answer_button_count(self):
        # Learning/relearning cards show only 3 buttons; a transformer returning Easy(4)
        # must clamp to the card's actual button count so it doesn't mis-schedule.
        entries = [_Entry(100, "x", lambda c, e: 4)]
        assert fold_ease(object(), 1, entries, ease_max=3) == 3


class TestEasePipeline:
    def test_add_remove_transformer(self):
        p = EasePipeline()
        assert p.has_transformers() is False
        p.add_transformer("x", lambda c, e: 2)
        assert p.has_transformers() is True
        assert p.compute_ease(object(), 4) == 2
        p.remove_transformer("x")
        assert p.has_transformers() is False
        assert p.compute_ease(object(), 4) == 4

    def test_install_wraps_answer_card_and_uninstall_restores(self):
        from aqt.reviewer import Reviewer

        p = EasePipeline()
        p.add_transformer("force_hard", lambda c, e: 2, priority=10)
        p.install()
        try:
            r = Reviewer(card=object())
            r._answerCard(4)  # user pressed Easy; transformer forces Hard(2)
            assert r.answered_with == [2]
        finally:
            p.uninstall()

        r2 = Reviewer(card=object())
        r2._answerCard(4)
        assert r2.answered_with == [4]  # original behavior restored

    def test_answer_card_clamps_to_scheduler_button_count(self):
        import types

        from aqt.reviewer import Reviewer

        p = EasePipeline()
        p.add_transformer("force_easy", lambda c, e: 4, priority=10)
        p.install()
        try:
            r = Reviewer(card=object())
            # A (re)learning card exposes only 3 answer buttons.
            r.mw = types.SimpleNamespace(
                col=types.SimpleNamespace(
                    sched=types.SimpleNamespace(answerButtons=lambda card: 3)
                )
            )
            r._answerCard(
                1
            )  # transformer forces Easy(4) -> clamped to 3 (no Easy button)
            assert r.answered_with == [3]
        finally:
            p.uninstall()

    def test_install_is_idempotent(self):
        from aqt.reviewer import Reviewer

        p = EasePipeline()
        p.install()
        p.install()
        try:
            r = Reviewer(card=object())
            r._answerCard(3)
            assert r.answered_with == [
                3
            ]  # no transformers -> unchanged, not double-wrapped
        finally:
            p.uninstall()
