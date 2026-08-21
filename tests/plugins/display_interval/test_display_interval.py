"""Tests for the display_interval feature (interval formatting + the grading-bar label).

The plugin no longer uses the card-webview injector: it drives the reviewer's persistent
BOTTOM (grading) bar directly off the show-question / show-answer hooks via
``anki_compat.reviewer_bottom_eval``. These tests stub that eval to capture the JS and hand
the plugin a tiny fake context (ease pipeline + a settings object carrying ``text_color``).
"""

from __future__ import annotations

import types

from conftest import FakeCard

from omnia.core import anki_compat
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.plugins.display_interval import DisplayIntervalPlugin
from omnia.plugins.display_interval.logic import format_interval


def _plugin_with_ctx(
    ease: EasePipeline, text_color: str = "#c62828"
) -> DisplayIntervalPlugin:
    """A plugin whose ``_ctx`` exposes just the ease pipeline + a ``text_color`` setting."""
    plugin = DisplayIntervalPlugin()
    plugin._ctx = types.SimpleNamespace(
        ease=ease, settings=types.SimpleNamespace(text_color=text_color)
    )
    return plugin


class TestFormatInterval:
    def test_format_interval_buckets(self):
        assert format_interval(0) == "0"
        assert format_interval(30) == "<1m"
        assert format_interval(90) == "<2m"  # sub-10-minute -> "<Nm" (reference parity)
        assert format_interval(60 * 12) == "12m"  # 10+ minutes -> "Nm"
        assert format_interval(3600) == "1h"
        assert format_interval(86_400) == "1d"
        assert format_interval(86_400 * 45) == "2mo"
        assert format_interval(86_400 * 400) == "1.1y"


class TestAnswerLabel:
    def test_answer_evals_pipeline_ease_and_formats(self, monkeypatch):
        captured: dict = {}
        evals: list[str] = []

        def fake_next_ivl(card, ease):
            captured["ease"] = ease
            return 3 * 86_400  # 3 days

        monkeypatch.setattr(anki_compat, "next_interval_seconds", fake_next_ivl)
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", evals.append)

        ease = EasePipeline()
        ease.add_transformer("guard", lambda card, e: 2, priority=200)  # force Hard
        _plugin_with_ctx(ease)._on_answer(FakeCard(id=1))

        assert captured["ease"] == 2  # used the pipeline-effective ease, not raw Good
        assert len(evals) == 1
        assert "interval: 3d" in evals[0]

    def test_answer_uses_nondestructive_preview(self, monkeypatch):
        # The label must PREVIEW (apply=False): an apply-aware, staged transformer (like
        # typed_accuracy) is peeked, not consumed, so the real grade still receives it.
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: 86_400
        )
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", lambda _js: None)

        pending = {1: 2}

        def staged(card, ease, *, apply=True):
            cid = card.id
            return pending.pop(cid, None) if apply else pending.get(cid)

        ease = EasePipeline()
        ease.add_transformer("typed", staged, priority=100)
        _plugin_with_ctx(ease)._on_answer(FakeCard(id=1))
        # The preview did NOT consume the staged ease; the real grade still gets it.
        assert ease.compute_ease(FakeCard(id=1), 3) == 2

    def test_answer_noop_when_no_interval(self, monkeypatch):
        evals: list[str] = []
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: None
        )
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", evals.append)

        _plugin_with_ctx(EasePipeline())._on_answer(FakeCard(id=1))
        assert evals == []  # nothing pushed to the bar when there's no interval

    def test_answer_tolerates_extra_hook_args(self, monkeypatch):
        # The reviewer_did_show_answer hook passes the card first; extra args must not break it.
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: 86_400
        )
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", lambda _js: None)
        _plugin_with_ctx(EasePipeline())._on_answer(FakeCard(id=1), "extra")  # no raise


class TestQuestionHides:
    def test_question_evals_hide(self, monkeypatch):
        evals: list[str] = []
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", evals.append)

        _plugin_with_ctx(EasePipeline())._on_question(FakeCard(id=1))
        assert len(evals) == 1
        assert "__TA_NEXT_IVL" in evals[0]
        assert "none" in evals[0]  # hides via display = 'none'


class TestRenderJs:
    def test_render_js_substitutes_text_and_color(self):
        js = _plugin_with_ctx(EasePipeline(), text_color="#00ff00")._render_js(
            "interval: 3d"
        )
        assert '"interval: 3d"' in js  # JSON-encoded label
        assert '"#00ff00"' in js  # JSON-encoded configured colour
        assert "__TEXT__" not in js  # placeholders fully substituted
        assert "__COLOR__" not in js
        assert "__TA_NEXT_IVL" in js  # load-bearing element id preserved


class TestEnableDisable:
    def _ctx(self):
        return types.SimpleNamespace(
            ease=EasePipeline(),
            settings=types.SimpleNamespace(text_color="#c62828"),
        )

    def test_enable_subscribes_both_hooks(self, gui_hooks):
        DisplayIntervalPlugin().on_enable(self._ctx())
        assert gui_hooks.reviewer_did_show_question.count() == 1
        assert gui_hooks.reviewer_did_show_answer.count() == 1

    def test_disable_unsubscribes_and_removes_label(self, gui_hooks, monkeypatch):
        evals: list[str] = []
        monkeypatch.setattr(anki_compat, "reviewer_bottom_eval", evals.append)

        plugin = DisplayIntervalPlugin()
        ctx = self._ctx()
        plugin.on_enable(ctx)
        plugin.on_disable(ctx)

        assert gui_hooks.reviewer_did_show_question.count() == 0
        assert gui_hooks.reviewer_did_show_answer.count() == 0
        # Teardown removes the label from the bottom bar (REMOVE snippet references its id).
        assert any("__TA_NEXT_IVL" in js for js in evals)


class TestTemplateExpose:
    """card_will_show prepends window.omniaIntervals to the ANSWER html for template JS."""

    def _plugin(self, ease, expose=True, monkeypatch=None, seconds=3 * 86_400):
        import types as _t

        if monkeypatch is not None:
            monkeypatch.setattr(
                anki_compat, "next_interval_seconds", lambda card, e: seconds
            )
        plugin = DisplayIntervalPlugin()
        plugin._ctx = _t.SimpleNamespace(
            ease=ease,
            settings=_t.SimpleNamespace(
                text_color="#c62828", expose_to_templates=expose
            ),
        )
        return plugin

    @staticmethod
    def _active_pipeline():
        """A pipeline with a passthrough transformer — exposure requires >=1 active."""
        ease = EasePipeline()
        ease.add_transformer("noop", lambda card, e: e, priority=100)
        return ease

    def test_answer_prepends_intervals_script(self, monkeypatch):
        plugin = self._plugin(self._active_pipeline(), monkeypatch=monkeypatch)
        out = plugin._on_card_will_show(
            "<b>ans</b>", FakeCard(id=1, ivl=6), "reviewAnswer"
        )
        assert out.endswith("<b>ans</b>")  # original html intact, script prepended
        assert '"next_days": 3.0' in out
        assert '"next_seconds": 259200' in out
        assert '"next_label": "3d"' in out
        assert '"current_days": 6' in out
        assert '"state": "review"' in out
        assert "omnia:intervals" in out  # CustomEvent handshake

    def test_state_reflects_relearning(self, monkeypatch):
        # A just-lapsed card still carries a positive day ivl (e.g. 1) — the state field is
        # what lets a template put it back on the "still shaky" branch.
        plugin = self._plugin(self._active_pipeline(), monkeypatch=monkeypatch)
        out = plugin._on_card_will_show(
            "a", FakeCard(id=1, ivl=1, type=3), "reviewAnswer"
        )
        assert '"state": "relearning"' in out and '"current_days": 1' in out

    def test_uses_pipeline_preview_ease(self, monkeypatch):
        captured: dict = {}

        def fake_next(card, ease):
            captured["ease"] = ease
            return 86_400

        monkeypatch.setattr(anki_compat, "next_interval_seconds", fake_next)
        ease = EasePipeline()
        ease.add_transformer("guard", lambda card, e: 2, priority=200)  # force Hard
        plugin = self._plugin(ease)
        plugin._on_card_will_show("x", FakeCard(id=1), "reviewAnswer")
        assert captured["ease"] == 2  # overdue_guard-style transform reflected

    def test_question_side_untouched(self, monkeypatch):
        plugin = self._plugin(self._active_pipeline(), monkeypatch=monkeypatch)
        assert plugin._on_card_will_show("q", FakeCard(id=1), "reviewQuestion") == "q"

    def test_flag_off_leaves_html_untouched(self, monkeypatch):
        plugin = self._plugin(
            self._active_pipeline(), expose=False, monkeypatch=monkeypatch
        )
        assert plugin._on_card_will_show("a", FakeCard(id=1), "reviewAnswer") == "a"

    def test_no_interval_available_passes_through(self, monkeypatch):
        monkeypatch.setattr(anki_compat, "next_interval_seconds", lambda c, e: None)
        plugin = self._plugin(self._active_pipeline())
        assert plugin._on_card_will_show("a", FakeCard(id=1), "reviewAnswer") == "a"

    def test_no_active_transformers_passes_through(self, monkeypatch):
        # overdue_guard / typed_accuracy off -> the preview adds nothing over the current
        # interval, so nothing is injected and the template's own fallback applies.
        plugin = self._plugin(EasePipeline(), monkeypatch=monkeypatch)
        assert plugin._on_card_will_show("a", FakeCard(id=1), "reviewAnswer") == "a"

    def test_enable_subscribes_card_will_show(self, gui_hooks):
        import types as _t

        plugin = DisplayIntervalPlugin()
        ctx = _t.SimpleNamespace(
            ease=EasePipeline(),
            settings=_t.SimpleNamespace(text_color="#c62828", expose_to_templates=True),
        )
        plugin.on_enable(ctx)
        assert gui_hooks.card_will_show.count() == 1
        plugin.on_disable(ctx)
        assert gui_hooks.card_will_show.count() == 0
