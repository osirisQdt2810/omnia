"""Tests for the typed_accuracy feature (pure logic + an end-to-end seam test)."""

from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest
from conftest import FakeCard

from omnia.core.plugin import AddonPaths, PluginContext
from omnia.core.providers import ProviderHub
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.core.reviewer.web_injector import WebInjector, build_message
from omnia.features.typed_accuracy import TypedAccuracyPlugin
from omnia.features.typed_accuracy.logic import accuracy_ratio, decide_ease


class TestAccuracyLogic:
    def test_accuracy_ratio(self):
        assert accuracy_ratio(0, 0, 0) == 0.0
        assert accuracy_ratio(8, 1, 1) == 0.8

    def test_decide_ease_pass_good_fail_hard(self):
        assert decide_ease(0.9, 0.7, "good") == 3
        assert decide_ease(0.9, 0.7, "easy") == 4
        assert decide_ease(0.5, 0.7, "good") == 2  # fail -> Hard


def _context(settings):
    return PluginContext(
        plugin_id="typed_accuracy",
        settings=settings,
        log=logging.getLogger("omnia.test"),
        ease=EasePipeline(),
        web=WebInjector(),
        providers=ProviderHub(),
        paths=AddonPaths(Path("/x"), Path("/x/web"), Path("/x/uf")),
        config=None,  # not exercised by this seam test
        reload_self=lambda: None,
    )


@pytest.fixture
def current_card(monkeypatch):
    import aqt

    card = FakeCard(id=42)
    monkeypatch.setattr(
        aqt, "mw", types.SimpleNamespace(reviewer=types.SimpleNamespace(card=card))
    )
    return card


class TestTypedAccuracyPlugin:
    def test_pass_grades_good_via_pipeline(self, current_card):
        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="good"))
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)

        handled, _ = ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )
        assert handled is True
        assert (
            ctx.ease.compute_ease(current_card, 1) == 3
        )  # staged Good overrides the request

    def test_fail_grades_hard(self, current_card):
        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="easy"))
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.4}), None
        )
        assert ctx.ease.compute_ease(current_card, 3) == 2  # Hard

    def test_other_card_not_affected(self, current_card):
        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        TypedAccuracyPlugin().on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )
        other = FakeCard(id=999)
        assert ctx.ease.compute_ease(other, 3) == 3  # no pending ease for this card

    def test_question_clears_stale_pending(self, current_card):
        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )
        plugin._on_question(current_card)  # showing the question wipes staged ease
        assert ctx.ease.compute_ease(current_card, 3) == 3

    def test_disable_fully_tears_down(self, current_card, gui_hooks):
        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )
        assert plugin._pending  # staged
        plugin.on_disable(ctx)
        assert ctx.ease.compute_ease(current_card, 3) == 3  # transformer removed
        assert not plugin._pending  # cleared
        assert ctx.web.collect_js("answer") == ""  # asset removed
        assert gui_hooks.reviewer_did_show_question.count() == 0  # hook removed
