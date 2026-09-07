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

    def test_registers_every_action_with_the_configured_shortcuts(self, anki):
        AudioSpeedPlugin().on_enable(_ctx())
        by_label = {a.label: a.shortcut for a in anki.actions}
        assert by_label == {
            "Audio: speed up": "]",
            "Audio: slow down": "[",
            "Audio: reset speed": "Ctrl+]",
            "Audio: speed up (front side)": "Alt+]",
            "Audio: slow down (front side)": "Alt+[",
            "Audio: speed up (back side)": "Shift+]",
            "Audio: slow down (back side)": "Shift+[",
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
        assert config.writes[-1] == (
            "audio_speed",
            {"rate": 1.25, "answer_rate": 1.25},
        )
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

    def test_the_persisted_write_carries_only_the_two_rates(self, anki):
        """ADR-010: write only what changed, never the whole section."""
        config = _Config()
        AudioSpeedPlugin().on_enable(_ctx(config=config, answer_rate=1.0))
        _action(anki, "Audio: speed up").callback(False)
        section, values = config.writes[-1]
        assert section == "audio_speed" and set(values) == {"rate", "answer_rate"}


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

    def test_it_hands_the_live_page_back_at_normal_speed(self, anki):
        # Dropping the injector entry only stops FUTURE renders from carrying a rate. The
        # applier already installed in the page on screen keeps forcing the old rate onto every
        # <audio> the template creates — the exact answer-side case this plugin exists to fix —
        # until the reviewer webview is rebuilt. Zero resets it to 1.0 and stands it down, so a
        # template that sets a rate of its own is not overridden by a disabled plugin either.
        plugin = AudioSpeedPlugin()
        ctx = _ctx(rate=2.0)
        plugin.on_enable(ctx)

        plugin.on_disable(ctx)

        assert anki.evals[-1] == push_rate_js(0.0)

    def test_a_shortcut_fired_after_disable_is_a_no_op(self, anki):
        plugin = AudioSpeedPlugin()
        ctx = _ctx()
        plugin.on_enable(ctx)
        up = _action(anki, "Audio: speed up")
        plugin.on_disable(ctx)
        before = list(anki.mpv)

        up.callback(False)

        assert anki.mpv == before


class TestPersistence:
    def test_a_press_that_actually_moves_the_rate_is_saved(self, anki):
        config = _Config()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(
            _ctx(config=config, rate=1.0, answer_rate=1.0, step=0.1, max_rate=3.0)
        )

        _action(anki, "Audio: speed up").callback(False)

        assert config.writes == [("audio_speed", {"rate": 1.1, "answer_rate": 1.1})]

    def test_a_press_at_the_ceiling_writes_nothing(self, anki):
        # Auto-repeat: holding the key at a bound clamps to the same rate over and over, and
        # every save is a collection-backed write that syncs. Nothing changed, so nothing is
        # written.
        config = _Config()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(
            _ctx(config=config, rate=3.0, answer_rate=3.0, step=0.1, max_rate=3.0)
        )

        for _ in range(5):
            _action(anki, "Audio: speed up").callback(False)

        assert config.writes == []


class TestPushJs:
    def test_applies_when_the_applier_exists_and_parks_the_rate_when_it_does_not(self):
        js = push_rate_js(1.5)
        assert "S.apply(r)" in js
        assert "__omniaAudioSpeedPending=r" in js
        assert js.endswith("(1.5);")


class TestPerSideSpeeds:
    """The two rates only become two speeds because each render adopts its own side's."""

    def test_each_side_renders_with_its_own_rate(self, anki):
        web = _Web()
        AudioSpeedPlugin().on_enable(_ctx(web=web, rate=1.5, answer_rate=2.0))
        dynamic = web.dynamic["audio_speed"]

        assert dynamic["question"](None) == push_rate_js(1.5)
        assert dynamic["answer"](None) == push_rate_js(2.0)

    def test_a_render_hands_its_side_rate_to_mpv_too(self, anki):
        web = _Web()
        AudioSpeedPlugin().on_enable(_ctx(web=web, rate=1.5, answer_rate=2.0))
        dynamic = web.dynamic["audio_speed"]

        dynamic["answer"](None)
        assert anki.mpv[-1] == 2.0
        dynamic["question"](None)
        assert anki.mpv[-1] == 1.5

    def test_a_front_only_press_leaves_the_back_alone(self, anki):
        web, config = _Web(), _Config()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(
            _ctx(web=web, config=config, rate=1.0, answer_rate=1.0, step=0.5)
        )

        _action(anki, "Audio: speed up (front side)").callback(False)

        assert config.writes[-1] == ("audio_speed", {"rate": 1.5, "answer_rate": 1.0})
        assert web.dynamic["audio_speed"]["question"](None) == push_rate_js(1.5)
        assert web.dynamic["audio_speed"]["answer"](None) == push_rate_js(1.0)

    def test_a_back_only_press_while_the_question_shows_changes_nothing_audible_yet(
        self, anki
    ):
        # The stored rate moves, but only the side on screen can be made to sound different
        # now; the back side's new speed arrives with the flip.
        web = _Web()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(web=web, rate=1.0, answer_rate=1.0, step=0.5))
        web.dynamic["audio_speed"]["question"](None)

        _action(anki, "Audio: speed up (back side)").callback(False)

        assert anki.mpv[-1] == 1.0
        assert anki.evals[-1] == push_rate_js(1.0)
        assert anki.tooltips[-1] == "Audio speed 1.5× (back)"
        assert web.dynamic["audio_speed"]["answer"](None) == push_rate_js(1.5)

    def test_a_back_only_press_while_the_answer_shows_takes_effect_at_once(self, anki):
        web = _Web()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(web=web, rate=1.0, answer_rate=1.0, step=0.5))
        web.dynamic["audio_speed"]["answer"](None)

        _action(anki, "Audio: speed up (back side)").callback(False)

        assert anki.mpv[-1] == 1.5
        assert anki.evals[-1] == push_rate_js(1.5)

    def test_the_plain_shortcut_keeps_a_gap_the_user_set(self, anki):
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(rate=1.0, answer_rate=2.0, step=0.5))

        _action(anki, "Audio: speed up").callback(False)

        assert anki.tooltips[-1] == "Audio speed — front 1.5×, back 2.5×"

    def test_reset_returns_both_sides(self, anki):
        config = _Config()
        plugin = AudioSpeedPlugin()
        plugin.on_enable(_ctx(config=config, rate=2.0, answer_rate=0.75))

        _action(anki, "Audio: reset speed").callback(False)

        assert config.writes[-1] == ("audio_speed", {"rate": 1.0, "answer_rate": 1.0})


class TestUpgradeFromASingleRate:
    """A stored section written before the back side existed must not go quiet."""

    def test_a_stored_rate_with_no_answer_rate_is_adopted_by_both_sides(self, anki):
        web = _Web()
        # `_ctx(rate=...)` leaves answer_rate unset, which is exactly the pre-upgrade section.
        AudioSpeedPlugin().on_enable(_ctx(web=web, rate=1.75))
        dynamic = web.dynamic["audio_speed"]

        assert dynamic["question"](None) == push_rate_js(1.75)
        assert dynamic["answer"](None) == push_rate_js(1.75)

    def test_the_adopted_rate_is_written_out_immediately(self, anki):
        # Until the key exists the generic settings form shows the field's 1.0 default, and
        # saving that form would write the default over the speed the user is hearing.
        config = _Config()
        AudioSpeedPlugin().on_enable(_ctx(config=config, rate=1.75))
        assert config.writes == [("audio_speed", {"rate": 1.75, "answer_rate": 1.75})]

    def test_a_section_that_already_has_both_is_left_alone(self, anki):
        config = _Config()
        AudioSpeedPlugin().on_enable(_ctx(config=config, rate=1.75, answer_rate=1.0))
        assert config.writes == []

    def test_nothing_is_written_when_the_speed_is_not_remembered(self, anki):
        config = _Config()
        AudioSpeedPlugin().on_enable(
            _ctx(config=config, rate=1.75, remember_rate=False)
        )
        assert config.writes == []
