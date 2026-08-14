"""Tests for the note-maintenance Anki glue: the Browser entry point and its hook lifecycle.

The plugin's only hook is the Browser context menu, and enabling the feature must never edit a
note by itself — so what is pinned here is: the hook goes on at enable and comes off at disable,
a run reads the SELECTED notes into ``NoteView``s, each note is planned with ITS note type's
settings, and the resulting plan reaches the preview (the dialog itself is Qt and is stubbed
out). ``run_in_background`` is conftest's synchronous QueryOp stub, so the scan runs inline.
"""

from __future__ import annotations

import sys
import types

import pytest

from omnia.core import anki_compat
from omnia.plugins.note_maintenance import NoteMaintenancePlugin, _note_views
from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings
from omnia.plugins.note_maintenance.registry import registered_tasks

_BROWSER_HOOK = "browser_will_show_context_menu"


class _FakeNote:
    """A note exposing the two things the view builder uses: ``items()`` + ``note_type()``."""

    def __init__(self, fields: dict[str, str], note_type: str = "Vocab") -> None:
        self._fields = dict(fields)
        self._note_type = note_type

    def items(self):
        return list(self._fields.items())

    def note_type(self):
        return {"name": self._note_type}


class _FakeBrowser:
    """Stands in for Anki's Browser: it only has to report the selected note ids."""

    def __init__(self, note_ids: list[int]) -> None:
        self._note_ids = list(note_ids)

    def selectedNotes(self):
        return list(self._note_ids)


@pytest.fixture
def tooltips(monkeypatch):
    """Capture the ``aqt.utils.tooltip`` messages the glue reports to the user."""
    messages: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "aqt.utils",
        types.SimpleNamespace(
            tooltip=lambda text, *_a, **_k: messages.append(text),
            showWarning=lambda *_a, **_k: None,
        ),
    )
    return messages


def _plugin(settings: NoteMaintenanceSettings | None = None) -> NoteMaintenancePlugin:
    """An enabled plugin over ``settings`` (Vocab set up with its default tasks otherwise)."""
    plugin = NoteMaintenancePlugin()
    plugin.on_enable(types.SimpleNamespace(settings=settings or _configured("Vocab")))
    return plugin


def _configured(note_type: str, **tasks) -> NoteMaintenanceSettings:
    """Settings with ``note_type`` switched on (its default tasks unless told otherwise)."""
    return NoteMaintenanceSettings(
        note_types={note_type: {"enable": True, "tasks": dict(tasks)}}
    )


def _all_tasks_off() -> NoteMaintenanceSettings:
    return _configured(
        "Vocab", **{task_id: {"enable": False} for task_id in registered_tasks()}
    )


def _updates(change) -> dict[str, str]:
    """The change as ``{field: new value}`` — what a run WOULD leave the note holding."""
    return {field.field: field.after for field in change.fields}


class TestHookLifecycle:
    def test_enable_subscribes_the_browser_hook_disable_removes_it(self, gui_hooks):
        ctx = types.SimpleNamespace(settings=_configured("Vocab"))
        plugin = NoteMaintenancePlugin()

        plugin.on_enable(ctx)
        assert getattr(gui_hooks, _BROWSER_HOOK).count() == 1

        plugin.on_disable(ctx)
        assert getattr(gui_hooks, _BROWSER_HOOK).count() == 0

    def test_a_disabled_plugin_leaves_no_hook_behind_after_a_re_enable(self, gui_hooks):
        ctx = types.SimpleNamespace(settings=_configured("Vocab"))
        plugin = NoteMaintenancePlugin()

        plugin.on_enable(ctx)
        plugin.on_disable(ctx)
        plugin.on_enable(ctx)
        plugin.on_disable(ctx)

        assert getattr(gui_hooks, _BROWSER_HOOK).count() == 0

    def test_enabling_runs_nothing_by_itself(self, gui_hooks, monkeypatch):
        # The feature is user-initiated: enabling must not read or plan anything.
        monkeypatch.setattr(
            anki_compat,
            "run_in_background",
            lambda *_a, **_k: pytest.fail("enable must not start a run"),
        )
        NoteMaintenancePlugin().on_enable(
            types.SimpleNamespace(settings=_configured("Vocab"))
        )


class TestNoteViews:
    def test_reads_the_notes_keeping_the_note_type_field_order(self, monkeypatch):
        notes = {7: _FakeNote({"Word": "plunge", "Synonyms": "dive"})}
        monkeypatch.setattr(
            anki_compat, "get_note_or_none", lambda nid, col=None: notes.get(nid)
        )

        views = _note_views([7])

        assert len(views) == 1
        assert views[0].note_id == 7
        assert views[0].note_type == "Vocab"
        assert list(views[0].fields.items()) == [
            ("Word", "plunge"),
            ("Synonyms", "dive"),
        ]

    def test_skips_a_note_that_is_gone(self, monkeypatch):
        notes = {7: _FakeNote({"Word": "plunge"})}
        monkeypatch.setattr(
            anki_compat, "get_note_or_none", lambda nid, col=None: notes.get(nid)
        )

        assert [view.note_id for view in _note_views([7, 8])] == [7]


class TestMaintainNotes:
    def test_plans_the_selected_notes_and_previews_the_result(
        self, gui_hooks, monkeypatch, tooltips
    ):
        notes = {
            5: _FakeNote(
                {"Synonyms": "modest (ˈmɒdɪst), meek (miːk)", "SynonymsNoIPA": ""}
            )
        }
        monkeypatch.setattr(
            anki_compat, "get_note_or_none", lambda nid, col=None: notes.get(nid)
        )
        plugin = _plugin()
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([5]))

        assert len(previewed) == 1
        plan = previewed[0]
        assert [note.note_id for note in plan] == [5]
        assert _updates(plan.notes[0]) == {"SynonymsNoIPA": "modest, meek"}
        assert tooltips == []

    def test_a_task_config_this_version_cannot_parse_still_runs(
        self, gui_hooks, monkeypatch, tooltips
    ):
        # A hand-edited features.toml (or a section written by a NEWER Omnia and synced down)
        # must not throw out of the menu action into Anki's traceback dialog: the broken task
        # falls back to its defaults and the run goes ahead.
        notes = {
            5: _FakeNote(
                {"Synonyms": "modest (ˈmɒdɪst), meek (miːk)", "SynonymsNoIPA": ""}
            )
        }
        monkeypatch.setattr(
            anki_compat, "get_note_or_none", lambda nid, col=None: notes.get(nid)
        )
        plugin = _plugin(_configured("Vocab", strip_ipa={"order": "whenever"}))
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([5]))

        assert [note.note_id for note in previewed[0]] == [5]

    def test_each_note_is_planned_with_its_own_note_types_settings(
        self, gui_hooks, monkeypatch, tooltips
    ):
        # The Browser selection routinely spans note types; the one with no settings must be
        # reported rather than quietly contributing nothing.
        notes = {
            5: _FakeNote(
                {"Synonyms": "modest (ˈmɒdɪst)", "SynonymsNoIPA": ""}, "Vocab"
            ),
            6: _FakeNote({"Synonyms": "meek (miːk)", "SynonymsNoIPA": ""}, "Basic"),
        }
        monkeypatch.setattr(
            anki_compat, "get_note_or_none", lambda nid, col=None: notes.get(nid)
        )
        plugin = _plugin()
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([5, 6]))

        plan = previewed[0]
        assert [note.note_id for note in plan] == [5]
        assert [(entry.note_type, entry.note_count) for entry in plan.skipped] == [
            ("Basic", 1)
        ]

    def test_an_empty_selection_reports_and_scans_nothing(self, gui_hooks, tooltips):
        plugin = _plugin()
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([]))

        assert previewed == []
        assert tooltips and "select" in tooltips[0].lower()

    def test_reports_when_every_task_is_switched_off(self, gui_hooks, tooltips):
        plugin = _plugin(_all_tasks_off())
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([5]))

        assert previewed == []
        assert tooltips and "task" in tooltips[0].lower()

    def test_reports_when_no_note_type_is_set_up_and_scans_nothing(
        self, gui_hooks, tooltips
    ):
        plugin = _plugin(NoteMaintenanceSettings())
        previewed: list = []
        plugin._preview = lambda plan, parent: previewed.append(plan)

        plugin.maintain_notes(_FakeBrowser([5]))

        assert previewed == []
        assert tooltips and "note type" in tooltips[0].lower()

    def test_a_plan_with_nothing_to_change_opens_no_dialog(self, gui_hooks, tooltips):
        from omnia.plugins.note_maintenance.runner import ChangePlan

        # _preview imports the Qt dialog only when there IS something to show, so an empty
        # plan must return before that (it would blow up headless otherwise).
        _plugin()._preview(ChangePlan(), parent=None)

        assert tooltips and "no maintenance" in tooltips[0].lower()

    def test_an_empty_plan_says_which_note_types_it_passed_over(
        self, gui_hooks, tooltips
    ):
        # "Nothing happened" and "nothing happened because this note type is not set up" look
        # identical to the user, and the second one is why the feature seemed broken.
        from omnia.plugins.note_maintenance.runner import (
            ChangePlan,
            SkippedNotes,
            SkipReason,
        )

        plan = ChangePlan(
            (), (SkippedNotes("Basic", SkipReason.UNCONFIGURED, note_count=3),)
        )

        _plugin()._preview(plan, parent=None)

        assert tooltips and "Basic" in tooltips[0] and "3 note(s)" in tooltips[0]
