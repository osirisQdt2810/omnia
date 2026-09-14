"""Tests for the Sync dialog's ops, driven on a real :class:`SyncDialog` without Qt.

Constructed with ``__new__`` and handed exactly what the ops touch. That is deliberate rather
than lazy: these methods contain the decisions — which preference to write back when a socket
refuses to open, which card a sentence lands in — and testing a copy of them would test the copy.
Everything Qt (``_show``, ``_render``) is replaced, because none of it decides anything.
"""

from __future__ import annotations

import pytest
from aqt_stubs import install_gui_stubs

install_gui_stubs()

from omnia.core.sync import MachineSettings  # noqa: E402
from omnia.gui.sync import session  # noqa: E402
from omnia.gui.sync.dialog import SyncDialog  # noqa: E402
from omnia.gui.sync.html import PanelState  # noqa: E402


class _Repo:
    def __init__(self, **sections: dict) -> None:
        self.sections: dict[str, dict] = dict(sections)

    def raw_section(self, name: str) -> dict:
        return dict(self.sections.get(name, {}))

    def update_section(self, name: str, values: dict) -> None:
        self.sections.setdefault(name, {}).update(values)


@pytest.fixture
def dialog(monkeypatch):
    """A SyncDialog with its Qt half replaced, sharing already on."""
    repo = _Repo(sync={"key": "123456789", "sharing": True, "port": 8767})
    panel = SyncDialog.__new__(SyncDialog)
    panel._repo = repo
    panel._settings = MachineSettings(repo)
    panel._inventory = lambda: None
    panel._offer = None
    panel._state = PanelState(sharing=True, machine_id="1", access_code="2")
    shown: list[PanelState] = []
    monkeypatch.setattr(SyncDialog, "_show", lambda self, state: shown.append(state))
    panel.shown = shown
    yield panel
    session.stop()


class TestRegeneratingTheAccessCode:
    def test_it_replaces_the_code_and_says_the_id_has_not_changed(
        self, dialog, monkeypatch
    ):
        monkeypatch.setattr(session, "start", lambda identity, inventory: True)
        before = dialog._settings.identity().key

        dialog._on_regenerate({})

        assert dialog._settings.identity().key != before
        state = dialog.shown[-1]
        assert "new access code" in state.local_status
        assert "the ID has not changed" in state.local_status

    def test_a_restart_that_fails_turns_the_preference_off_too(
        self, dialog, monkeypatch
    ):
        # Windows is where this bites: `allow_reuse_address` is off there, so rebinding straight
        # after a close can fail. Leaving `sharing = true` behind would have the next profile
        # open silently retry a port that is taken, while the panel says off — the preference
        # and the screen disagreeing with nothing on screen to explain it.
        monkeypatch.setattr(session, "start", lambda identity, inventory: False)

        dialog._on_regenerate({})

        assert dialog._settings.identity().sharing is False
        assert dialog._repo.sections["sync"]["sharing"] is False

    def test_a_restart_that_fails_says_which_port_and_what_to_do(
        self, dialog, monkeypatch
    ):
        monkeypatch.setattr(session, "start", lambda identity, inventory: False)

        dialog._on_regenerate({})

        state = dialog.shown[-1]
        assert "8767" in state.local_status
        assert "already in use" in state.local_status
        # And NOT in the other machine's slot, which is where it used to land.
        assert state.status == ""

    def test_a_failed_restart_shows_no_id_to_hand_out(self, dialog, monkeypatch):
        monkeypatch.setattr(session, "start", lambda identity, inventory: False)

        dialog._on_regenerate({})

        assert dialog.shown[-1].machine_id == ""
        assert dialog.shown[-1].access_code == ""


class TestTheSharingSwitch:
    def test_turning_it_off_leaves_the_other_machine_alone(self, dialog):
        dialog._state = PanelState(
            sharing=True, peer_id="168 604 419 56", peer_code="123 456 789"
        )

        dialog._on_sharing({"on": False})

        state = dialog.shown[-1]
        assert state.sharing is False
        assert state.peer_id == "168 604 419 56"
        assert state.peer_code == "123 456 789"

    def test_a_port_that_is_taken_reports_under_this_computer(
        self, dialog, monkeypatch
    ):
        monkeypatch.setattr(session, "start", lambda identity, inventory: False)

        answer = dialog._on_sharing({"on": True})

        assert answer == {"ok": False}
        assert dialog._settings.identity().sharing is False
        assert "already in use" in dialog.shown[-1].local_status
        assert dialog.shown[-1].status == ""


class TestCheckingTheOtherMachine:
    def test_a_mistyped_id_is_answered_without_dialling_anything(
        self, dialog, monkeypatch
    ):
        # The check runs off the Qt thread and costs the full timeout against a sleeping machine.
        # A number that cannot be an ID must never get that far.
        called: list[str] = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.run_in_background",
            lambda *a, **k: called.append("dialled"),
        )

        dialog._on_connect({"id": "168 604 419 57", "code": "123 456 789"})

        assert called == []
        assert "mistyped" in dialog.shown[-1].status

    def test_a_bad_access_code_is_answered_the_same_way(self, dialog, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.run_in_background",
            lambda *a, **k: called.append("dialled"),
        )

        dialog._on_connect({"id": "168 604 419 56", "code": "12"})

        assert called == []
        assert "9 digits" in dialog.shown[-1].status

    def test_both_numbers_are_remembered_for_next_time(self, dialog, monkeypatch):
        monkeypatch.setattr(
            "omnia.core.anki_compat.run_in_background", lambda *a, **k: None
        )

        dialog._on_connect({"id": "168 604 419 56", "code": "123 456 789"})

        stored = dialog._repo.sections["sync"]
        assert stored["peer_id"] == "168 604 419 56"
        assert stored["peer_code"] == "123 456 789"
