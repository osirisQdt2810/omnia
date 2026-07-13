"""Tests for the auto_flip feature (delay helper + scheduling/flip behavior)."""

from __future__ import annotations

import types

from omnia.plugins.auto_flip import AutoFlipPlugin
from omnia.plugins.auto_flip.config import AutoFlipDeckOverride, AutoFlipSettings
from omnia.plugins.auto_flip.countdown import (
    build_countdown_js,
    clear_countdown_js,
    mark_countdown_cancelled_js,
)
from omnia.plugins.auto_flip.logic import (
    delay_ms,
    effective_delays,
    parse_mpv_range_extra_seconds,
)


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

    def test_use_global_returns_global_delays_despite_override(self):
        # use_global -> defer to the global delays even though a per-deck row exists.
        settings = AutoFlipSettings(
            delay_question_seconds=3,
            delay_answer_seconds=2,
            per_deck={
                "9": AutoFlipDeckOverride(
                    use_global=True,
                    delay_question_seconds=9,
                    delay_answer_seconds=9,
                )
            },
        )
        assert effective_delays(settings, 9) == (True, 3000, 2000)

    def test_use_global_wins_over_disabled(self):
        # use_global keeps the deck on (global delays) regardless of enabled.
        settings = AutoFlipSettings(
            delay_question_seconds=1,
            delay_answer_seconds=1,
            per_deck={"9": AutoFlipDeckOverride(use_global=True, enabled=False)},
        )
        assert effective_delays(settings, 9) == (True, 1000, 1000)

    def test_deck_delays_when_not_use_global_and_enabled(self):
        settings = AutoFlipSettings(
            delay_question_seconds=3,
            delay_answer_seconds=3,
            per_deck={
                "9": AutoFlipDeckOverride(
                    use_global=False,
                    enabled=True,
                    delay_question_seconds=6,
                    delay_answer_seconds=7,
                )
            },
        )
        assert effective_delays(settings, 9) == (True, 6000, 7000)

    def test_disabled_and_not_use_global_is_off(self):
        settings = AutoFlipSettings(
            per_deck={"9": AutoFlipDeckOverride(use_global=False, enabled=False)}
        )
        enabled, _q_ms, _a_ms = effective_delays(settings, 9)
        assert enabled is False


class TestParseMpvRangeExtraSeconds:
    def test_no_range_returns_zero(self):
        assert parse_mpv_range_extra_seconds("[sound:hello.mp3]") == 0.0
        assert parse_mpv_range_extra_seconds("") == 0.0

    def test_requires_both_markers(self):
        # The myview.mpv marker without --range= (and vice versa) yields nothing.
        assert parse_mpv_range_extra_seconds("myview.mpv 00:00:01,000") == 0.0
        assert parse_mpv_range_extra_seconds("--range=00:00:01,000-00:00:02,000") == 0.0

    def test_single_range_comma_millis(self):
        cmd = "myview.mpv --range=00:00:01,000-00:00:04,500 clip.mkv"
        assert parse_mpv_range_extra_seconds(cmd) == 3.5

    def test_dot_millis_supported(self):
        cmd = "myview.mpv --range=00:01:00.000-00:01:02.250 clip.mkv"
        assert parse_mpv_range_extra_seconds(cmd) == 2.25

    def test_multiple_ranges_sum(self):
        cmd = (
            "myview.mpv --range=00:00:00,000-00:00:02,000 a.mkv "
            "myview.mpv --range=00:00:10,000-00:00:13,000 b.mkv"
        )
        assert parse_mpv_range_extra_seconds(cmd) == 5.0

    def test_hours_minutes_seconds(self):
        cmd = "myview.mpv --range=01:00:00,000-01:02:03,000 clip.mkv"
        assert parse_mpv_range_extra_seconds(cmd) == 123.0


class _FakeTimer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _settings(**kw):
    from omnia.plugins.auto_flip.config import AutoFlipSettings

    return AutoFlipSettings(**kw)


def _fake_mw(monkeypatch, schedule, calls, *, card=None):
    import aqt

    def timer(ms, cb, repeat):
        t = _FakeTimer()
        schedule.append((ms, cb, t))
        return t

    reviewer = types.SimpleNamespace(
        state="question",
        card=card,
        _showAnswer=lambda: calls.append("show_answer"),
        _answerCard=lambda ease: calls.append(("answer", ease)),
    )
    menu_tools = types.SimpleNamespace(
        addAction=lambda action: calls.append(("menu_add", action)),
        removeAction=lambda action: calls.append(("menu_remove", action)),
    )
    mw = types.SimpleNamespace(
        progress=types.SimpleNamespace(timer=timer),
        reviewer=reviewer,
        form=types.SimpleNamespace(menuTools=menu_tools),
    )
    monkeypatch.setattr(aqt, "mw", mw)
    # Reset the av_player queue + the onEnterKey wrap so tests don't leak into each other.
    aqt.sound.av_player._enqueued = []
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
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

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
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

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
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

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
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        gui_hooks.reviewer_did_show_question.fire(object())
        pending = schedule[-1][2]
        # User answers before the timer fires -> filter hook returns ease, timer stops.
        result = gui_hooks.reviewer_will_answer_card.fire(
            (3, False), object(), object()
        )
        assert result == (3, False)
        assert pending.stopped is True

        plugin.on_disable(ctx)

    def test_disable_unsubscribes_all_hooks(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(settings=_settings(wait_for_audio=False))
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        assert gui_hooks.reviewer_did_show_question.count() == 1
        plugin.on_disable(ctx)
        for hook in (
            gui_hooks.reviewer_did_show_question,
            gui_hooks.reviewer_did_show_answer,
            gui_hooks.reviewer_will_answer_card,
            gui_hooks.deck_browser_will_show_options_menu,
        ):
            assert hook.count() == 0


class TestAudioAwareArming:
    def test_no_sounds_arms_immediately(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=True, delay_question_seconds=4)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        # An empty sounds list on the will-play hook -> arm right away.
        gui_hooks.reviewer_will_play_question_sounds.fire(object(), [])
        assert schedule and schedule[-1][0] == 4000

        plugin.on_disable(ctx)

    def test_with_sounds_defers_until_audio_ends(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls, card=object())

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=True, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._active = (
            True  # activate WITHOUT rearming — this test drives the audio-hook path
        )

        # A card WITH audio: nothing is scheduled until the audio queue drains.
        gui_hooks.reviewer_will_play_question_sounds.fire(object(), ["snd.mp3"])
        assert schedule == []
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 1
        assert schedule[-1][0] == 5000

        plugin.on_disable(ctx)

    def test_audio_end_waits_for_remaining_queue(self, gui_hooks, monkeypatch):
        import aqt

        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls, card=object())

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=True, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._active = (
            True  # activate WITHOUT rearming — this test drives the audio-hook path
        )
        gui_hooks.reviewer_will_play_question_sounds.fire(object(), ["a.mp3", "b.mp3"])

        # First clip ends but another remains queued -> still don't arm.
        aqt.sound.av_player._enqueued = ["b.mp3"]
        gui_hooks.av_player_did_end_playing.fire(object())
        assert schedule == []
        # Last clip ends, queue empty -> arm.
        aqt.sound.av_player._enqueued = []
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 1

        plugin.on_disable(ctx)

    def test_audio_path_does_not_subscribe_show_hooks(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(settings=_settings(wait_for_audio=True))
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        assert gui_hooks.reviewer_did_show_question.count() == 0
        assert gui_hooks.reviewer_will_play_question_sounds.count() == 1
        assert gui_hooks.av_player_did_end_playing.count() == 1
        plugin.on_disable(ctx)
        for hook in (
            gui_hooks.reviewer_will_play_question_sounds,
            gui_hooks.reviewer_will_play_answer_sounds,
            gui_hooks.av_player_did_end_playing,
        ):
            assert hook.count() == 0


class TestFilteredDeckResolution:
    def test_override_keyed_off_original_deck_for_filtered_card(
        self, gui_hooks, monkeypatch
    ):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        # Card is in a filtered deck (did=999) but its home deck (odid=42) is configured.
        card = types.SimpleNamespace(did=999, odid=42)
        ctx = types.SimpleNamespace(
            settings=_settings(
                wait_for_audio=False,
                per_deck={"42": AutoFlipDeckOverride(enabled=False)},
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        gui_hooks.reviewer_did_show_question.fire(card)
        # The home deck (odid=42) disables auto-flip -> nothing scheduled.
        assert schedule == []

        plugin.on_disable(ctx)

    def test_no_odid_falls_back_to_did(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        card = types.SimpleNamespace(did=42, odid=0)
        ctx = types.SimpleNamespace(
            settings=_settings(
                wait_for_audio=False,
                per_deck={"42": AutoFlipDeckOverride(enabled=False)},
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        gui_hooks.reviewer_did_show_question.fire(card)
        assert schedule == []

        plugin.on_disable(ctx)


class TestMpvRangeWiring:
    def test_clip_duration_added_to_question_delay(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        card = types.SimpleNamespace(
            did=1,
            odid=0,
            question=lambda: "myview.mpv --range=00:00:01,000-00:00:04,000 clip.mkv",
            answer=lambda: "",
        )
        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_question_seconds=2)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        gui_hooks.reviewer_did_show_question.fire(card)
        # 2s base delay + 3s clip duration = 5000 ms.
        assert schedule and schedule[-1][0] == 5000

        plugin.on_disable(ctx)


class TestRuntimeToggle:
    def test_toggle_off_cancels_pending_timer(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        gui_hooks.reviewer_did_show_question.fire(object())
        pending = schedule[-1][2]

        plugin._on_toggle(False)  # Ctrl+J off
        assert pending.stopped is True

        # While off, showing a question arms nothing.
        gui_hooks.reviewer_did_show_question.fire(object())
        assert len(schedule) == 1

        plugin.on_disable(ctx)

    def test_toggle_on_rearms_current_side(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        card = types.SimpleNamespace(did=1, odid=0)
        mw = _fake_mw(monkeypatch, schedule, calls, card=card)
        mw.reviewer.state = "answer"

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_answer_seconds=4)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        plugin._on_toggle(False)
        plugin._on_toggle(True)  # mid-card resume -> re-arm the answer side

        assert schedule and schedule[-1][0] == 4000

        plugin.on_disable(ctx)


class TestTwoStageEnterCancel:
    def test_first_enter_cancels_second_enter_acts(self, gui_hooks, monkeypatch):
        import aqt

        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)
        evals = _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        gui_hooks.reviewer_did_show_question.fire(object())
        pending = schedule[-1][2]

        reviewer = aqt.reviewer.Reviewer()
        before = reviewer.enter_calls
        # First Enter: cancel the pending auto-flip, mark the ring, don't act.
        reviewer.onEnterKey()
        assert pending.stopped is True
        assert reviewer.enter_calls == before  # no real action yet
        assert any("3b82f6" in js for js in evals)  # ring marked "cancelled"

        # Second Enter: perform Anki's real action.
        reviewer.onEnterKey()
        assert reviewer.enter_calls == before + 1

        plugin.on_disable(ctx)

    def test_enter_without_pending_timer_acts_immediately(self, gui_hooks, monkeypatch):
        import aqt

        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        ctx = types.SimpleNamespace(settings=_settings(wait_for_audio=False))
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test

        reviewer = aqt.reviewer.Reviewer()
        before = reviewer.enter_calls
        reviewer.onEnterKey()  # no timer pending -> straight to Anki's action
        assert reviewer.enter_calls == before + 1

        plugin.on_disable(ctx)

    def test_disable_restores_original_enter_key(self, gui_hooks, monkeypatch):
        import aqt

        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)

        original = aqt.reviewer.Reviewer.onEnterKey
        ctx = types.SimpleNamespace(settings=_settings(wait_for_audio=False))
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(
            True
        )  # auto-flip starts suspended each session; Ctrl+J on for the test
        assert aqt.reviewer.Reviewer.onEnterKey is not original
        plugin.on_disable(ctx)
        assert aqt.reviewer.Reviewer.onEnterKey is original


class TestCancelledRingJs:
    def test_marks_ring_and_stops_interval(self):
        js = mark_countdown_cancelled_js()
        assert "omnia-autoflip-timer" in js
        assert "clearInterval" in js
        assert "3b82f6" in js


class TestAutoGradeChain:
    def test_grade_preserves_next_cards_timer(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        card1 = types.SimpleNamespace(did=1, odid=0)
        card2 = types.SimpleNamespace(did=1, odid=0)
        mw = _fake_mw(monkeypatch, schedule, calls)
        _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(
                wait_for_audio=False,
                delay_question_seconds=3,
                delay_answer_seconds=2,
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(True)

        # Card 1: question timer fires -> flip to the answer side.
        mw.reviewer.state = "question"
        gui_hooks.reviewer_did_show_question.fire(card1)
        schedule[-1][1]()
        assert "show_answer" in calls

        # Card 1: answer side arms the grade timer.
        mw.reviewer.state = "answer"
        gui_hooks.reviewer_did_show_answer.fire(card1)
        grade_timer = schedule[-1][2]

        # Grading card 1 advances to card 2, which (like real Anki) fires the answer-card
        # filter hook then shows card 2's question -> arms card 2's timer.
        def answer_card(ease):
            calls.append(("answer", ease))
            gui_hooks.reviewer_will_answer_card.fire((ease, False), object(), object())
            mw.reviewer.state = "question"
            gui_hooks.reviewer_did_show_question.fire(card2)

        mw.reviewer._answerCard = answer_card

        # Fire card 1's grade timer -> _grade -> answer -> re-arm card 2.
        schedule[-1][1]()
        assert ("answer", 3) in calls

        next_timer = schedule[-1][2]
        assert next_timer is not grade_timer
        # The just-armed next card's timer must survive (auto-grade chain continues).
        assert next_timer.stopped is False
        assert schedule[-1][0] == 3000  # card 2 armed with the question delay

        plugin.on_disable(ctx)


class TestAudioEnterCancelThenNextSide:
    def test_enter_cancel_does_not_disable_answer_side_autograde(
        self, gui_hooks, monkeypatch
    ):
        import aqt

        schedule: list = []
        calls: list = []
        card = types.SimpleNamespace(did=1, odid=0)
        mw = _fake_mw(monkeypatch, schedule, calls, card=card)
        _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(
                wait_for_audio=True,
                delay_question_seconds=5,
                delay_answer_seconds=4,
            )
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._active = True  # audio path: activate WITHOUT rearming

        # Question side with audio -> defer, then arm once the queue drains.
        mw.reviewer.state = "question"
        gui_hooks.reviewer_will_play_question_sounds.fire(card, ["q.mp3"])
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 1

        # First Enter cancels the pending question auto-flip (sets _enter_cancelled=True).
        reviewer = aqt.reviewer.Reviewer()
        reviewer.onEnterKey()
        assert schedule[-1][2].stopped is True

        # The answer side is presented (sounds play, then end). The enter-cancel flag must
        # be reset on the new side so answer-side auto-grade still arms.
        mw.reviewer.state = "answer"
        gui_hooks.reviewer_will_play_answer_sounds.fire(card, ["a.mp3"])
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 2  # grade timer armed despite the earlier Enter cancel
        assert schedule[-1][0] == 4000

        plugin.on_disable(ctx)


class TestReviewerExitTeardown:
    def test_leaving_review_cancels_pending_timer(self, gui_hooks, monkeypatch):
        schedule: list = []
        calls: list = []
        _fake_mw(monkeypatch, schedule, calls)
        _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=False, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)
        plugin._on_toggle(True)

        gui_hooks.reviewer_did_show_question.fire(object())
        pending = schedule[-1][2]

        # Leaving the reviewer for the deck list tears the pending timer down.
        gui_hooks.state_did_change.fire("deckBrowser", "review")
        assert pending.stopped is True

        plugin.on_disable(ctx)


class TestToggleRespectsWaitForAudio:
    def test_toggle_on_while_audio_playing_defers_arming(self, gui_hooks, monkeypatch):
        import aqt

        schedule: list = []
        calls: list = []
        card = types.SimpleNamespace(did=1, odid=0)
        mw = _fake_mw(monkeypatch, schedule, calls, card=card)
        _record_reviewer_eval(monkeypatch)

        ctx = types.SimpleNamespace(
            settings=_settings(wait_for_audio=True, delay_question_seconds=5)
        )
        plugin = AutoFlipPlugin()
        plugin.on_enable(ctx)

        # A clip is still playing when the user hits Ctrl+J on the question side.
        mw.reviewer.state = "question"
        aqt.sound.av_player._enqueued = ["q.mp3"]
        plugin._on_toggle(True)
        assert schedule == []  # did not arm immediately; deferred to av_player drain

        # When the queue drains, the deferred side arms.
        aqt.sound.av_player._enqueued = []
        gui_hooks.av_player_did_end_playing.fire(object())
        assert len(schedule) == 1
        assert schedule[-1][0] == 5000

        plugin.on_disable(ctx)
