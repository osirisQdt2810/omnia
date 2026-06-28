"""Tests for the display_interval feature (formatting + the overlay JS builder)."""

from __future__ import annotations

import types

from conftest import FakeCard

from omnia.core import anki_compat
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.features.display_interval import DisplayIntervalPlugin
from omnia.features.display_interval.logic import format_interval


class TestFormatInterval:
    def test_format_interval_buckets(self):
        assert format_interval(30) == "<1m"
        assert format_interval(90) == "2m"
        assert format_interval(3600) == "1h"
        assert format_interval(86_400) == "1d"
        assert format_interval(86_400 * 45) == "2mo"
        assert format_interval(86_400 * 400) == "1.1y"


class TestDisplayIntervalPlugin:
    def test_overlay_js_uses_pipeline_ease_and_formats(self, monkeypatch):
        captured = {}

        def fake_next_ivl(card, ease):
            captured["ease"] = ease
            return 3 * 86_400  # 3 days

        monkeypatch.setattr(anki_compat, "next_interval_seconds", fake_next_ivl)

        ease = EasePipeline()
        ease.add_transformer("guard", lambda card, e: 2, priority=200)  # force Hard
        ctx = types.SimpleNamespace(ease=ease)

        js = DisplayIntervalPlugin._overlay_js(ctx, FakeCard(id=1))
        assert "next: 3d" in js
        assert captured["ease"] == 2  # used the pipeline-effective ease, not raw Good

    def test_overlay_js_none_when_no_interval(self, monkeypatch):
        monkeypatch.setattr(
            anki_compat, "next_interval_seconds", lambda card, ease: None
        )
        ctx = types.SimpleNamespace(ease=EasePipeline())
        assert DisplayIntervalPlugin._overlay_js(ctx, FakeCard(id=1)) is None

    def test_disable_removes_asset_and_dynamic(self):
        from omnia.core.reviewer.web_injector import WebInjector

        web = WebInjector()
        ctx = types.SimpleNamespace(web=web, ease=EasePipeline())
        plugin = DisplayIntervalPlugin()
        plugin.on_enable(ctx)
        assert "display_interval" in web._dynamic
        assert web.collect_js("question") != ""  # css asset registered
        plugin.on_disable(ctx)
        assert "display_interval" not in web._dynamic
        assert web.collect_js("answer") == ""
