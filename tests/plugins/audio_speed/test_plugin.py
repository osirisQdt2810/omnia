"""The plugin's glue: what enable registers, what a shortcut does, what disable leaves.

Anki is stubbed (``conftest``), so ``anki_compat`` is monkeypatched at the seam and the web
injector is a recorder. What these pin is the CONTRACT the reported bug was about: the rate is
pushed on every render of BOTH sides, it reaches mpv AND the webview, it is remembered, and a
machine where mpv is not the player still gets the HTML half without a crash.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnia.core import anki_compat
from omnia.core.reviewer.web_injector import WebAsset
from omnia.plugins.audio_speed import (
    AudioSpeedPlugin,
    build_speed_js,
    push_rate_js,
)
from omnia.plugins.audio_speed.config import AudioSpeedSettings


class _Web:
    """Records the injector calls a plugin makes; nothing is rendered."""

    def __init__(self) -> None:
        self.assets: dict[str, WebAsset] = {}
        self.dynamic: dict[str, dict] = {}
        self.removed: list[str] = []

    def add_asset(self, plugin_id, asset):
        self.assets[plugin_id] = asset

    def add_dynamic(self, plugin_id, *, on_question=None, on_answer=None):
        self.dynamic[plugin_id] = {"question": on_question, "answer": on_answer}

    def add_handler(self, *_a, **_k):
        pass

    def remove(self, plugin_id):
        self.removed.append(plugin_id)
        self.assets.pop(plugin_id, None)
        self.dynamic.pop(plugin_id, None)


class _Config:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def update_section(self, section, values):
        self.writes.append((section, dict(values)))


def _ctx(web=None, config=None, **overrides):
    settings = AudioSpeedSettings(**overrides)
    return SimpleNamespace(
        plugin_id="audio_speed",
        settings=settings,
        web=web if web is not None else _Web(),
        config=config if config is not None else _Config(),
        log=None,
    )


@pytest.fixture
def anki(monkeypatch):
    """Stub the four anki_compat seams and record every call."""
    rec = SimpleNamespace(
        mpv=[], evals=[], tooltips=[], actions=[], removed=[], mpv_ok=True
    )

    def set_mpv_speed(rate):
        rec.mpv.append(rate)
        return rec.mpv_ok

    def add_action(label, callback, *, checkable=False, checked=False, shortcut=None):
        action = SimpleNamespace(label=label, callback=callback, shortcut=shortcut)
        rec.actions.append(action)
        return action

    monkeypatch.setattr(anki_compat, "set_mpv_speed", set_mpv_speed)
    monkeypatch.setattr(anki_compat, "reviewer_eval", rec.evals.append)
    monkeypatch.setattr(anki_compat, "show_tooltip", rec.tooltips.append)
    monkeypatch.setattr(anki_compat, "add_tools_menu_action", add_action)
    monkeypatch.setattr(anki_compat, "remove_tools_menu_action", rec.removed.append)
    return rec


def _action(rec, label):
    return next(a for a in rec.actions if a.label == label)


class TestEnable:
    def test_injects_the_applier_on_both_sides(self, anki):
        web = _Web()
        AudioSpeedPlugin().on_enable(_ctx(web=web))

        asset = web.assets["audio_speed"]
        assert asset.question_js == asset.answer_js == build_speed_js()
        assert "__omniaAudioSpeed" in asset.question_js

    def test_pushes_the_current_rate_on_every_render_of_both_sides(self, anki):
        """The half the old add-on lacked: the answer side's fresh elements get the rate."""
        web = _Web()
        AudioSpeedPlugin().on_enable(_ctx(web=web, rate=1.7))

        providers = web.dynamic["audio_speed"]
        assert providers["question"](None) == push_rate_js(1.7)
        assert providers["answer"](None) == push_rate_js(1.7)

    def test_sets_mpv_once_on_enable_to_the_remembered_rate(self, anki):
        AudioSpeedPlugin().on_enable(_ctx(rate=1.7))
        assert anki.mpv == [1.7]

    def test_registers_three_actions_with_the_configured_shortcuts(self, anki):
        AudioSpeedPlugin().on_enable(
            _ctx(speed_up_shortcut="]", slow_down_shortcut="[", reset_shortcut="Ctrl+]")
        )
        by_label = {a.label: a.shortcut for a in anki.actions}
        assert by_label == {
            "Audio: speed up": "]",
            "Audio: slow down": "[",
            "Audio: reset speed": "Ctrl+]",
        }

    def test_remember_off_starts_at_normal_speed_whatever_was_stored(self, anki):
        AudioSpeedPlugin().on_enable(_ctx(rate=2.5, remember_rate=False))
        assert anki.mpv == [1.0]

    def test_a_stored_rate_outside_the_bounds_is_clamped_on_start(self, anki):
        AudioSpeedPlugin().on_enable(_ctx(rate=4.0, max_rate=2.0))
        assert anki.mpv == [2.0]


class TestShortcuts:
    def test_speed_up_reaches_mpv_the_webview_the_tooltip_and_the_config(self, anki):
        web, config = _Web(), _Config()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(web=web, config=config, rate=1.0, step=0.25))

        _action(anki, "Audio: speed up").callback(False)

        assert anki.mpv[-1] == 1.25
        assert anki.evals[-1] == push_rate_js(1.25)
        assert anki.tooltips[-1] == "Audio speed 1.25×"
        assert config.writes[-1] == ("audio_speed", {"rate": 1.25})
        # and the NEXT render of either side carries the new rate
        assert web.dynamic["audio_speed"]["answer"](None) == push_rate_js(1.25)

    def test_slow_down_and_reset(self, anki):
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(rate=1.0, step=0.5))

        _action(anki, "Audio: slow down").callback(False)
        assert anki.mpv[-1] == 0.5
        _action(anki, "Audio: reset speed").callback(False)
        assert anki.mpv[-1] == 1.0
        assert anki.tooltips[-1] == "Audio speed 1×"

    def test_remember_off_never_writes_the_config(self, anki):
        config = _Config()
        AudioSpeedPlugin().on_enable(_ctx(config=config, remember_rate=False))
        _action(anki, "Audio: speed up").callback(False)
        assert config.writes == []

    def test_tooltip_can_be_silenced(self, anki):
        AudioSpeedPlugin().on_enable(_ctx(show_tooltip=False))
        _action(anki, "Audio: speed up").callback(False)
        assert anki.tooltips == []

    def test_the_persisted_write_is_a_one_key_merge(self, anki):
        """ADR-010: write only what changed, never the whole section."""
        config = _Config()
        AudioSpeedPlugin().on_enable(_ctx(config=config))
        _action(anki, "Audio: speed up").callback(False)
        section, values = config.writes[-1]
        assert section == "audio_speed" and set(values) == {"rate"}


class TestWithoutMpv:
    """Qt player chosen, or a Windows install without mpv: the HTML half must still work."""

    def test_no_crash_and_the_webview_still_gets_the_rate(self, anki):
        anki.mpv_ok = False
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(rate=1.0, step=0.5))

        _action(anki, "Audio: speed up").callback(False)

        assert anki.evals[-1] == push_rate_js(1.5)

    def test_the_tooltip_says_why_only_html_audio_changed(self, anki):
        anki.mpv_ok = False
        AudioSpeedPlugin().on_enable(_ctx(step=0.5))
        _action(anki, "Audio: speed up").callback(False)
        assert "mpv is not the active player" in anki.tooltips[-1]

    def test_a_reviewer_eval_failure_is_swallowed(self, anki, monkeypatch):
        """Pressing ] before any card is up must not raise out of the Tools menu."""

        def boom(_js):
            raise RuntimeError("no reviewer")

        monkeypatch.setattr(anki_compat, "reviewer_eval", boom)
        AudioSpeedPlugin().on_enable(_ctx())
        _action(anki, "Audio: speed up").callback(False)  # does not raise


class TestDisable:
    def test_removes_the_injector_entry_and_the_actions_and_resets_mpv(self, anki):
        web = _Web()
        plugin = AudioSpeedPlugin()
        ctx = _ctx(web=web, rate=2.0)
        plugin.on_enable(ctx)

        plugin.on_disable(ctx)

        assert web.removed == ["audio_speed"]
        assert "audio_speed" not in web.assets and "audio_speed" not in web.dynamic
        assert anki.removed == anki.actions
        assert anki.mpv[-1] == 1.0

    def test_a_shortcut_fired_after_disable_is_a_no_op(self, anki):
        plugin = AudioSpeedPlugin()
        ctx = _ctx()
        plugin.on_enable(ctx)
        up = _action(anki, "Audio: speed up")
        plugin.on_disable(ctx)
        before = list(anki.mpv)

        up.callback(False)

        assert anki.mpv == before


class TestPushJs:
    def test_applies_when_the_applier_exists_and_parks_the_rate_when_it_does_not(self):
        js = push_rate_js(1.5)
        assert "S.apply(r)" in js
        assert "__omniaAudioSpeedPending=r" in js
        assert js.endswith("(1.5);")
