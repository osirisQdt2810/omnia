"""A plugin card showing what its plugin is doing, without the page knowing which plugins do.

The point of running a batch in the background is that Anki stays usable, and the cost of that is
that Anki's own modal dialog — the only progress surface it has — cannot be where it reports. So
the settings page grew one: each card asks its plugin for ``<id>.progress`` and draws whatever
answers. A plugin gains a bar by publishing a JobTracker, and this file learns nothing about it.
"""

from __future__ import annotations

import json

import pytest

from omnia.core import services
from omnia.core.progress import JobTracker, progress_service


class _Plugin:
    def __init__(self, plugin_id: str) -> None:
        self.id = plugin_id


class _Manager:
    def __init__(self, *plugin_ids: str) -> None:
        self._plugins = [_Plugin(pid) for pid in plugin_ids]

    def plugins(self):
        return self._plugins


@pytest.fixture
def dialog():
    """A SettingsDialog with its fields set directly — no Qt, no webview."""
    from aqt_stubs import install_gui_stubs

    install_gui_stubs()
    from omnia.gui.settings_dialog import SettingsDialog

    class _Dialog(SettingsDialog):
        def __init__(self) -> None:  # deliberately NOT SettingsDialog's
            self._manager = _Manager("smart_notes", "auto_flip")
            self._jobs_shown = {}
            self.evaluated: list[str] = []

        def eval_js(self, script: str) -> None:
            self.evaluated.append(script)

    return _Dialog()


@pytest.fixture
def tracker():
    """A published tracker, always withdrawn so no test leaks one into the next."""
    name = progress_service("smart_notes")
    published = JobTracker("Generating")
    services.provide(name, published)
    yield published
    services.revoke(name)


def _for(dialog, plugin_id: str) -> list:
    """The first push aimed at ``plugin_id``."""
    return next(p for p in _pushed(dialog) if p[0] == plugin_id)


def _pushed(dialog) -> list:
    """The (id, percent, text, stoppable) tuples the page was sent."""
    out = []
    for script in dialog.evaluated:
        if "setCardProgress(" not in script:
            continue
        args = script.split("setCardProgress(", 1)[1].rsplit(");", 1)[0]
        out.append(json.loads("[" + args + "]"))
    return out


class TestWhatReachesThePage:
    def test_a_running_job_is_drawn_with_its_count(self, dialog, tracker):
        tracker.start(300)
        tracker.record(142)

        dialog._jobs_tick()

        assert [
            "smart_notes",
            pytest.approx(47.333, abs=0.01),
            "Generating 142 of 300",
            True,
        ] in [[p[0], p[1], p[2], p[3]] for p in _pushed(dialog)]

    def test_a_plugin_with_no_progress_service_is_cleared_not_skipped(self, dialog):
        # Left alone, a card that had a bar keeps it — stuck at whatever it last said, forever.
        dialog._jobs_tick()

        assert ["auto_flip", None, "", False] in _pushed(dialog)

    def test_an_unknown_size_has_no_percentage(self, dialog, tracker):
        # The page draws a sweep instead of a bar pinned at 0%, which reads as stuck.
        tracker.start(0)

        dialog._jobs_tick()

        pushed = _for(dialog, "smart_notes")
        assert pushed[1] is None

    def test_an_unchanged_state_is_not_repainted(self, dialog, tracker):
        # This runs twice a second into a webview that is also rendering a settings grid.
        tracker.start(10)
        tracker.record(4)
        dialog._jobs_tick()
        before = len(dialog.evaluated)

        dialog._jobs_tick()

        assert len(dialog.evaluated) == before

    def test_a_finished_job_keeps_its_last_frame_but_cannot_be_stopped(
        self, dialog, tracker
    ):
        tracker.start(6)
        tracker.record(6)
        tracker.finish()

        dialog._jobs_tick()

        pushed = _for(dialog, "smart_notes")
        assert pushed[2], "the completed count vanished the moment it completed"
        assert pushed[3] is False

    def test_a_job_told_to_stop_is_not_stoppable_again(self, dialog, tracker):
        tracker.start(6)
        tracker.cancel()

        dialog._jobs_tick()

        pushed = _for(dialog, "smart_notes")
        assert pushed[3] is False
        assert "Stopping" in pushed[2]

    def test_a_tracker_that_raises_does_not_take_the_other_cards_with_it(self, dialog):
        # Any plugin may publish this; one that answers badly must not stop the poll.
        class Broken:
            def snapshot(self):
                raise RuntimeError("no")

        name = progress_service("smart_notes")
        services.provide(name, Broken())
        try:
            dialog._jobs_tick()
        finally:
            services.revoke(name)

        assert ["auto_flip", None, "", False] in _pushed(dialog)


class TestStopping:
    def test_it_asks_the_running_job_to_stop(self, dialog, tracker):
        tracker.start(9)

        dialog._on_stop_job({"id": "smart_notes"})

        assert tracker.cancelled is True

    def test_it_does_not_kill_the_job_outright(self, dialog, tracker):
        # Asks, never forces: the batch stops between notes so none is left half-generated.
        tracker.start(9)

        dialog._on_stop_job({"id": "smart_notes"})

        assert tracker.active is True

    def test_stopping_something_that_is_not_running_is_harmless(self, dialog):
        assert dialog._on_stop_job({"id": "auto_flip"}) == {}

    def test_stopping_an_unknown_plugin_is_harmless(self, dialog):
        assert dialog._on_stop_job({"id": "nothing_here"}) == {}

    def test_the_page_is_updated_without_waiting_for_the_next_tick(
        self, dialog, tracker
    ):
        # Otherwise the button sits enabled for up to half a second after it was pressed.
        tracker.start(9)

        dialog._on_stop_job({"id": "smart_notes"})

        assert _pushed(dialog), "nothing was redrawn"


class TestTheOpIsReachable:
    def test_the_page_sends_an_op_the_dialog_answers(self):
        from aqt_stubs import install_gui_stubs

        install_gui_stubs()
        from omnia.gui.settings_dialog import HANDLERS, SettingsDialog

        assert "stop-job" in HANDLERS
        assert callable(getattr(SettingsDialog, HANDLERS["stop-job"], None))
