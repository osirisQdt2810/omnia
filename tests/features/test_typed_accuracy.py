"""Tests for the typed_accuracy feature (pure logic + an end-to-end seam test)."""

from __future__ import annotations

import logging
import tempfile
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
from omnia.features.typed_accuracy.stats import (
    StatsStore,
    TypedResult,
    donut_svg,
    stats_card_html,
    summarize,
)


def _result(ratio: float, passed: bool, *, ts: float = 0.0, deck_id=None):
    return TypedResult(ts=ts, ratio=ratio, passed=passed, deck_id=deck_id)


class TestAccuracyLogic:
    def test_accuracy_ratio(self):
        assert accuracy_ratio(0, 0, 0) == 0.0
        assert accuracy_ratio(8, 1, 1) == 0.8

    def test_decide_ease_pass_good_fail_hard(self):
        assert decide_ease(0.9, 0.7, "good") == 3
        assert decide_ease(0.9, 0.7, "easy") == 4
        assert decide_ease(0.5, 0.7, "good") == 2  # fail -> Hard


def _context(settings, user_files: Path | None = None):
    # The plugin's stats store writes under user_files_dir, so it must be a real, writable
    # directory; default to a throwaway temp dir for tests that don't assert on the file.
    uf = user_files or Path(tempfile.mkdtemp())
    return PluginContext(
        plugin_id="typed_accuracy",
        settings=settings,
        log=logging.getLogger("omnia.test"),
        ease=EasePipeline(),
        web=WebInjector(),
        providers=ProviderHub(),
        paths=AddonPaths(uf, uf / "web", uf),
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
        assert (
            gui_hooks.overview_will_render_content.count() == 0
        )  # overview hook removed


class TestStatsSummary:
    def test_summarize_mixed(self):
        results = [_result(0.9, True) for _ in range(8)]
        results += [_result(0.3, False) for _ in range(2)]
        summary = summarize(results)
        assert summary.total == 10
        assert summary.passed == 8
        assert summary.failed == 2
        assert summary.pass_rate == pytest.approx(0.8)
        assert summary.avg_ratio == pytest.approx((0.9 * 8 + 0.3 * 2) / 10)

    def test_summarize_empty_is_zeros(self):
        summary = summarize([])
        assert summary.total == 0
        assert summary.passed == 0
        assert summary.failed == 0
        assert summary.pass_rate == 0.0
        assert summary.avg_ratio == 0.0


class TestDonutSvg:
    def test_renders_svg_with_percent(self):
        svg = donut_svg(summarize([_result(1.0, True), _result(1.0, True)]))
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
        assert "100%" in svg
        # CSP-safe: no external references.
        assert "http://" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "<script" not in svg

    def test_handles_zero_total(self):
        svg = donut_svg(summarize([]))
        assert svg.startswith("<svg")
        assert "0%" in svg


class TestStatsCardHtml:
    def test_card_embeds_donut_and_numbers(self):
        results = [_result(0.9, True) for _ in range(8)]
        results += [_result(0.3, False) for _ in range(2)]
        html = stats_card_html(summarize(results))
        assert "<svg" in html  # the donut is embedded
        assert "10 reviews" in html
        assert "Pass rate: 80%" in html
        assert "Avg accuracy:" in html


class TestStatsStore:
    def test_record_then_results_round_trips(self, tmp_path):
        store = StatsStore(tmp_path / "stats.json")
        store.record(0.9, 0.7, deck_id=1, now=100.0)
        store.record(0.4, 0.7, deck_id=1, now=200.0)
        results = store.results()
        assert [(r.ts, r.passed, r.deck_id) for r in results] == [
            (100.0, True, 1),
            (200.0, False, 1),
        ]
        # A fresh store over the same file reads the persisted history.
        assert len(StatsStore(tmp_path / "stats.json").results()) == 2

    def test_results_filter_by_deck(self, tmp_path):
        store = StatsStore(tmp_path / "stats.json")
        store.record(0.9, 0.7, deck_id=1, now=1.0)
        store.record(0.9, 0.7, deck_id=2, now=2.0)
        store.record(0.9, 0.7, deck_id=1, now=3.0)
        assert len(store.results(1)) == 2
        assert len(store.results(2)) == 1
        assert len(store.results()) == 3

    def test_trims_to_max_records(self, tmp_path):
        store = StatsStore(tmp_path / "stats.json", max_records=3)
        for i in range(5):
            store.record(0.9, 0.7, now=float(i))
        kept = store.results()
        assert len(kept) == 3
        assert [r.ts for r in kept] == [2.0, 3.0, 4.0]  # newest kept

    def test_missing_file_loads_empty(self, tmp_path):
        assert StatsStore(tmp_path / "absent.json").results() == []

    def test_corrupt_file_loads_empty(self, tmp_path):
        path = tmp_path / "stats.json"
        path.write_text("not json{", encoding="utf-8")
        store = StatsStore(path)
        assert store.results() == []
        # A subsequent record overwrites the corrupt file cleanly.
        store.record(0.9, 0.7, now=1.0)
        assert len(store.results()) == 1

    def test_clear_empties_history(self, tmp_path):
        store = StatsStore(tmp_path / "stats.json")
        store.record(0.9, 0.7, now=1.0)
        store.clear()
        assert store.results() == []


class TestOverviewStatsCard:
    def test_records_and_renders_deck_card(self, current_card, gui_hooks, tmp_path):
        import aqt

        from omnia.core.config.models import TypedAccuracySettings

        current_card.did = 7
        aqt.mw.col = types.SimpleNamespace(
            decks=types.SimpleNamespace(get_current_id=lambda: 7)
        )

        ctx = _context(TypedAccuracySettings(show_stats=True), user_files=tmp_path)
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)

        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )

        content = types.SimpleNamespace(table="<table></table>")
        aqt.gui_hooks.overview_will_render_content.fire(object(), content)
        assert "Typing accuracy" in content.table
        assert "1 reviews" in content.table
        # The result was persisted under the card's deck id.
        assert plugin._store is not None
        assert [r.deck_id for r in plugin._store.results()] == [7]

    def test_falls_back_to_all_decks_when_empty(
        self, current_card, gui_hooks, tmp_path
    ):
        import aqt

        from omnia.core.config.models import TypedAccuracySettings

        # The card under review is deck 7, but recorded history is for deck 99: the
        # deck-scoped query is empty, so the card falls back to all-decks data.
        store = StatsStore(tmp_path / "typed_accuracy_stats.json")
        store.record(0.9, 0.7, deck_id=99, now=1.0)
        current_card.did = 7
        aqt.mw.col = types.SimpleNamespace(
            decks=types.SimpleNamespace(get_current_id=lambda: 7)
        )

        ctx = _context(TypedAccuracySettings(show_stats=True), user_files=tmp_path)
        TypedAccuracyPlugin().on_enable(ctx)

        content = types.SimpleNamespace(table="")
        aqt.gui_hooks.overview_will_render_content.fire(object(), content)
        assert "1 reviews" in content.table  # the all-decks record showed up

    def test_disabled_when_show_stats_false(self, current_card, gui_hooks, tmp_path):
        import aqt

        from omnia.core.config.models import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(show_stats=False), user_files=tmp_path)
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95}), None
        )

        content = types.SimpleNamespace(table="<table></table>")
        aqt.gui_hooks.overview_will_render_content.fire(object(), content)
        assert content.table == "<table></table>"  # nothing appended
