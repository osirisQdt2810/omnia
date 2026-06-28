"""Tests for the auto_flip feature (delay helper + scheduling/flip behavior)."""

from __future__ import annotations

import types

from omnia.core.config.models import AutoFlipDeckOverride, AutoFlipSettings
from omnia.features.auto_flip import AutoFlipPlugin
from omnia.features.auto_flip.countdown import build_countdown_js, clear_countdown_js
from omnia.features.auto_flip.logic import delay_ms, effective_delays


class TestDelayMs:
    def test_delay_ms(self):
        assert delay_ms(3) == 3000
        assert delay_ms(0) == 0
        assert delay_ms(2.5) == 2500


class TestCountdownJs:
    def test_build_references_element_id_and_duration(self):
        js = build_countdown_js(3)
        assert "omnia-autoflip-timer" in js
        assert "setInterval" in js
        assert "3000" in js  # 3s expressed as ms in the script

    def test_build_honours_custom_element_id(self):
        js = build_countdown_js(2, element_id="custom-ring")
        assert "custom-ring" in js

    def test_clear_references_same_element_id(self):
        js = clear_countdown_js()
        assert "omnia-autoflip-timer" in js
        assert "clearInterval" in js


class TestEffectiveDelays:
    def test_global_delays_when_no_override(self):
        settings = AutoFlipSettings(delay_question_seconds=3, delay_answer_seconds=2)
        enabled, q_ms, a_ms = effective_delays(settings, 42)
        assert enabled is True
        assert (q_ms, a_ms) == (3000, 2000)

    def test_global_delays_when_deck_id_none(self):
        settings = AutoFlipSettings(delay_question_seconds=1, delay_answer_seconds=1)
        assert effective_delays(settings, None) == (True, 1000, 1000)

    def test_per_deck_override_values_used(self):
        settings = AutoFlipSettings(
            delay_question_seconds=3,
            delay_answer_seconds=3,
            per_deck={
                "42": AutoFlipDeckOverride(
                    delay_question_seconds=5, delay_answer_seconds=4
                )
            },
        )
        enabled, q_ms, a_ms = effective_delays(settings, 42)
        assert enabled is True
        assert (q_ms, a_ms) == (5000, 4000)

    def test_disabled_deck_returns_enabled_false(self):
        settings = AutoFlipSettings(per_deck={"7": AutoFlipDeckOverride(enabled=False)})
        enabled, _q_ms, _a_ms = effective_delays(settings, 7)
        assert enabled is False


class _FakeTimer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _settings(**kw):
    from omnia.core.config.models import AutoFlipSettings

    return AutoFlipSettings(**kw)


def _fake_mw(monkeypatch, schedule, calls):
    import aqt

    def timer(ms, cb, repeat):
        t = _FakeTimer()
        schedule.append((ms, cb, t))
        return t

    reviewer = types.SimpleNamespace(
        state="question",
        _showAnswer=lambda: calls.append("show_answer"),
        _answerCard=lambda ease: calls.append(("answer", ease)),
    )
    mw = types.SimpleNamespace(
        progress=types.SimpleNamespace(timer=timer), reviewer=reviewer
    )
    monkeypatch.setattr(aqt, "mw", mw)
    return mw


def _record_reviewer_eval(monkeypatch):
    """Capture JS pushed via ``anki_compat.reviewer_eval`` into a list."""
    from omnia.core import anki_compat

    evals: list[str] = []
    monkeypatch.setattr(anki_compat, "reviewer_eval", evals.append)
    return evals


class TestAutoFlipPlugin:
    def test_show_question_schedules_then_flips(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        mw = _fake_mw(monkeypatch, schedule, calls)
        evals = _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(
                delay_question_seconds=3,
                delay_answer_seconds=2,
                wait_for_audio=False,
                show_timer=True,
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        gui_hooks.reviewer_did_show_question.fire(object())
        assert schedule and schedule[-1][0] == 3000  # scheduled with question delay
        # show_timer is on -> a countdown was pushed into the reviewer webview.
        assert any("omnia-autoflip-timer" in js for js in evals)

        schedule[-1][1]()  # fire the timer callback -> _flip
        assert "show_answer" in calls

        # On the answer side, the grade timer fires Good (3).
        mw.reviewer.state = "answer"
        gui_hooks.reviewer_did_show_answer.fire(object())
        assert schedule[-1][0] == 2000
        schedule[-1][1]()
        assert ("answer", 3) in calls

        plugin.on_disable(ctx)

    def test_no_countdown_when_show_timer_off(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)
        evals = _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(
                delay_question_seconds=3, wait_for_audio=False, show_timer=False
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        gui_hooks.reviewer_did_show_question.fire(object())
        # Scheduling happened but no countdown was built.
        assert schedule and schedule[-1][0] == 3000
        assert not any("setInterval" in js for js in evals)

        plugin.on_disable(ctx)

    def test_disabled_deck_skips_auto_flip(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)
        _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(
                wait_for_audio=False,
                per_deck={"99": AutoFlipDeckOverride(enabled=False)},
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        gui_hooks.reviewer_did_show_question.fire(types.SimpleNamespace(did=99))
        # Override disables this deck -> nothing scheduled.
        assert schedule == []

        plugin.on_disable(ctx)

    def test_manual_answer_cancels_pending_timer(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        gui_hooks.reviewer_did_show_question.fire(object())
        pending = schedule[-1][2]
        # User answers before the timer fires -> filter hook returns ease, timer stops.
        result = gui_hooks.reviewer_will_answer_card.fire(
            (3, False), object(), object()
        )
        assert result == (3, False)
        assert pending.stopped is True

        plugin.on_disable(ctx)

    def test_wait_for_audio_restarts_timer_on_audio_end(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=True, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        gui_hooks.reviewer_did_show_question.fire(object())
        assert len(schedule) == 1  # initial question timer
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 2  # delay restarted from audio end
        assert schedule[-1][0] == 5000

        plugin.on_disable(ctx)

    def test_disable_unsubscribes_all_hooks(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(settings=_settings())
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        assert gui_hooks.reviewer_did_show_question.count() == 1
        plugin.on_disable(ctx)
        for hook in (
            gui_hooks.reviewer_did_show_question,
            gui_hooks.reviewer_did_show_answer,
            gui_hooks.reviewer_will_answer_card,
            gui_hooks.av_player_did_end_playing,
        ):
            assert hook.count() == 0
