"""Tests for the overdue_guard feature (pure rule + the Anki-glue transformer)."""

from __future__ import annotations

from omnia.plugins.overdue_guard import OverdueGuardPlugin
from omnia.plugins.overdue_guard.logic import OverdueRule


class TestOverdueRule:
    def test_not_overdue_when_interval_zero(self):
        rule = OverdueRule(ratio=0.8, min_days=2, force_again_after_days=0)
        assert rule.is_overdue(ivl_days=0, late_days=10) is False

    def test_not_overdue_below_min_days(self):
        rule = OverdueRule(ratio=0.1, min_days=5, force_again_after_days=0)
        assert rule.is_overdue(ivl_days=10, late_days=3) is False

    def test_overdue_when_ratio_and_min_days_met(self):
        rule = OverdueRule(ratio=0.8, min_days=2, force_again_after_days=0)
        assert rule.is_overdue(ivl_days=10, late_days=9) is True
        assert rule.is_overdue(ivl_days=10, late_days=5) is False  # 0.5 < 0.8

    def test_forced_ease_respects_explicit_again(self):
        rule = OverdueRule(0.5, 1, 0)
        assert rule.forced_ease(1, ivl_days=10, late_days=10, hard_ivl_days=3) is None

    def test_forced_ease_none_when_not_overdue(self):
        rule = OverdueRule(0.8, 2, 0)
        assert rule.forced_ease(3, ivl_days=10, late_days=1, hard_ivl_days=3) is None

    def test_forced_ease_hard_when_overdue(self):
        rule = OverdueRule(0.8, 2, 0)
        assert rule.forced_ease(3, ivl_days=10, late_days=10, hard_ivl_days=3) == 2

    def test_forced_ease_again_when_hard_interval_too_far(self):
        rule = OverdueRule(0.8, 2, force_again_after_days=7)
        assert rule.forced_ease(4, ivl_days=10, late_days=10, hard_ivl_days=30) == 1
        assert rule.forced_ease(4, ivl_days=10, late_days=10, hard_ivl_days=3) == 2


class TestOverdueGuardPlugin:
    def test_plugin_transformer_uses_anki_timing(self, monkeypatch):
        from conftest import FakeCard

        from omnia.core import anki_compat

        # 100-day interval, last reviewed 200 days ago -> very overdue.
        now_ms = 200 * 86_400_000
        monkeypatch.setattr(anki_compat, "card_last_review_ms", lambda card: 0)
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: 5 * 86_400
        )
        monkeypatch.setattr("time.time", lambda: now_ms / 1000)

        rule = OverdueRule(ratio=0.8, min_days=2, force_again_after_days=0)
        card = FakeCard(ivl=100, id=1)
        assert OverdueGuardPlugin._forced_ease(rule, card, 3) == 2  # forced Hard

    def test_plugin_does_not_flag_on_time_review(self, monkeypatch):
        # Regression: late_days is days PAST DUE, not elapsed-since-last-review. A card reviewed
        # exactly when due (elapsed == interval) is 0 days late and must NOT be forced.
        from conftest import FakeCard

        from omnia.core import anki_compat

        now_ms = 100 * 86_400_000  # reviewed 100 days ago, interval is 100 -> due today
        monkeypatch.setattr(anki_compat, "card_last_review_ms", lambda card: 0)
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: 5 * 86_400
        )
        monkeypatch.setattr("time.time", lambda: now_ms / 1000)

        rule = OverdueRule(ratio=0.8, min_days=2, force_again_after_days=0)
        card = FakeCard(ivl=100, id=1)
        assert (
            OverdueGuardPlugin._forced_ease(rule, card, 3) is None
        )  # on time, not overdue

    def test_disable_removes_transformer(self):
        import types

        from omnia.core.reviewer.ease_pipeline import EasePipeline
        from omnia.plugins.overdue_guard.config import OverdueGuardSettings

        ease = EasePipeline()
        ctx = types.SimpleNamespace(settings=OverdueGuardSettings(), ease=ease)
        plugin = OverdueGuardPlugin()
        plugin.on_enable(ctx)
        assert ease.has_transformers() is True
        plugin.on_disable(ctx)
        assert ease.has_transformers() is False
