"""Tests for per-note-type resolution and planning (pure logic).

What a note gets is decided by ITS OWN note type, so these pin the two halves of that: which
settings a given note's type resolves to, and what happens to a note whose type resolves to
none — which must be REPORTED, not silently nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnia.plugins.note_maintenance import registry as task_registry
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.note_types import NoteTypePlanner, NoteTypeScope
from omnia.plugins.note_maintenance.runner import NoteChange, SkipReason

_STAMP_ID = "stamp"


class _StampConfig(TaskConfigBase):
    """Config for the test task: what to write, into which field."""

    field: str = "Word"
    stamp: str = "!"


class _StampTask(MaintenanceTask):
    """Writes ``stamp`` into ``field`` — enough to observe WHICH settings a note got."""

    name = "Stamp"
    config_model = _StampConfig

    def process(self, note: NoteView) -> dict[str, str]:
        return {self.config.field: self.config.stamp}


def _stamp(field: str, stamp: str) -> dict[str, Any]:
    """The stored task map switching the test task on with the given options."""
    return {_STAMP_ID: {"enable": True, "field": field, "stamp": stamp}}


def _note(note_id: int, note_type: str, **fields: str) -> NoteView:
    return NoteView(note_id=note_id, note_type=note_type, fields=dict(fields))


def _updates(change: NoteChange) -> dict[str, str]:
    return {field.field: field.after for field in change.fields}


class TestNoteTypeScope:
    """Reading the stored map: never raises, whatever shape an entry has."""

    def test_a_configured_note_type_reports_its_tasks(self):
        scope = NoteTypeScope({"Vocab": {"enable": True, "tasks": _stamp("Word", "!")}})

        assert scope.is_configured("Vocab")
        assert scope.is_enabled("Vocab")
        assert scope.task_sections("Vocab") == _stamp("Word", "!")

    def test_an_unknown_note_type_is_not_configured(self):
        scope = NoteTypeScope({"Vocab": {"enable": True}})

        assert not scope.is_configured("Basic")
        assert scope.task_sections("Basic") == {}
        assert scope.stored_name("Basic") is None

    def test_the_name_matches_regardless_of_case_and_padding(self):
        scope = NoteTypeScope({" Vocab ": {"enable": True}})

        assert scope.stored_name("vocab") == " Vocab "
        assert scope.is_enabled("VOCAB")

    def test_a_first_spelling_wins_over_a_later_duplicate(self):
        scope = NoteTypeScope({"Vocab": {"enable": True}, "VOCAB": {"enable": False}})

        assert scope.stored_name("vocab") == "Vocab"
        assert scope.is_enabled("vocab")

    def test_an_entry_with_no_enable_key_counts_as_on(self):
        # Hand-written config: the settings exist because someone put them there.
        scope = NoteTypeScope({"Vocab": {"tasks": _stamp("Word", "!")}})

        assert scope.is_enabled("Vocab")

    def test_an_entry_that_is_not_a_table_reads_as_unconfigured(self):
        scope = NoteTypeScope({"Vocab": "a string"})

        # It IS stored (and so must survive a save), it just cannot be read as settings.
        assert scope.stored_name("Vocab") == "Vocab"
        assert not scope.is_configured("Vocab")
        assert not scope.is_enabled("Vocab")
        assert scope.task_sections("Vocab") == {}

    def test_a_tasks_value_that_is_not_a_table_reads_as_no_tasks(self):
        scope = NoteTypeScope({"Vocab": {"enable": True, "tasks": "nonsense"}})

        assert scope.task_sections("Vocab") == {}

    def test_the_names_keep_their_stored_order_and_spelling(self):
        scope = NoteTypeScope({"Vocab": {}, "Basic (and reversed)": {}})

        assert scope.names == ("Vocab", "Basic (and reversed)")


class TestNoteTypePlanner:
    """Which settings a note gets, and what is reported for one that gets none."""

    @pytest.fixture(autouse=True)
    def stamp_only(self):
        """Register ONLY the stamp task, so what a note type ran is unambiguous."""
        snapshot = dict(task_registry.TASK_REGISTRY)
        task_registry.TASK_REGISTRY.clear()
        task_registry.register_task(_STAMP_ID)(_StampTask)
        yield
        task_registry.TASK_REGISTRY.clear()
        task_registry.TASK_REGISTRY.update(snapshot)

    def test_each_note_is_planned_with_its_own_note_types_settings(self):
        planner = NoteTypePlanner(
            NoteTypeScope(
                {
                    "Vocab": {"enable": True, "tasks": _stamp("Word", "vocab!")},
                    "Kanji": {"enable": True, "tasks": _stamp("Reading", "kanji!")},
                }
            )
        )

        plan = planner.plan(
            [
                _note(1, "Vocab", Word="", Reading=""),
                _note(2, "Kanji", Word="", Reading=""),
            ]
        )

        assert [note.note_id for note in plan] == [1, 2]
        assert _updates(plan.notes[0]) == {"Word": "vocab!"}
        assert _updates(plan.notes[1]) == {"Reading": "kanji!"}

    def test_several_note_types_are_maintained_in_one_pass(self):
        planner = NoteTypePlanner(
            NoteTypeScope(
                {
                    "Vocab": {"enable": True, "tasks": _stamp("Word", "a")},
                    "Kanji": {"enable": True, "tasks": _stamp("Word", "b")},
                }
            )
        )

        plan = planner.plan([_note(1, "Vocab", Word=""), _note(2, "Kanji", Word="")])

        assert plan.note_count == 2
        assert plan.skipped == ()

    def test_a_note_type_with_no_settings_is_skipped_and_reported(self):
        planner = NoteTypePlanner(
            NoteTypeScope({"Vocab": {"enable": True, "tasks": _stamp("Word", "!")}})
        )

        plan = planner.plan(
            [
                _note(1, "Vocab", Word=""),
                _note(2, "Basic", Word=""),
                _note(3, "Basic", Word=""),
            ]
        )

        assert [note.note_id for note in plan] == [1]
        assert len(plan.skipped) == 1
        skipped = plan.skipped[0]
        assert (skipped.note_type, skipped.reason, skipped.note_count) == (
            "Basic",
            SkipReason.UNCONFIGURED,
            2,
        )
        assert "Basic" in plan.skip_summary
        assert plan.skipped_note_count == 2

    def test_an_unticked_note_type_is_skipped_as_disabled(self):
        planner = NoteTypePlanner(
            NoteTypeScope({"Vocab": {"enable": False, "tasks": _stamp("Word", "!")}})
        )

        plan = planner.plan([_note(1, "Vocab", Word="")])

        assert plan.is_empty
        assert plan.skipped[0].reason is SkipReason.DISABLED

    def test_a_note_type_with_every_task_off_is_skipped_as_such(self):
        planner = NoteTypePlanner(
            NoteTypeScope(
                {"Vocab": {"enable": True, "tasks": {_STAMP_ID: {"enable": False}}}}
            )
        )

        plan = planner.plan([_note(1, "Vocab", Word="")])

        assert plan.is_empty
        assert plan.skipped[0].reason is SkipReason.NO_TASKS

    def test_a_note_type_entry_that_is_not_a_table_is_skipped_not_raised(self):
        planner = NoteTypePlanner(NoteTypeScope({"Vocab": "a string"}))

        plan = planner.plan([_note(1, "Vocab", Word="")])

        assert plan.skipped[0].reason is SkipReason.UNCONFIGURED

    def test_a_note_with_no_resolvable_note_type_is_reported_readably(self):
        plan = NoteTypePlanner(NoteTypeScope({})).plan([_note(1, "", Word="")])

        assert plan.skipped[0].label == "(unknown note type)"
        assert "(unknown note type)" in plan.skip_summary

    def test_the_skips_are_counted_per_note_type_in_selection_order(self):
        planner = NoteTypePlanner(
            NoteTypeScope({"Kanji": {"enable": False, "tasks": _stamp("Word", "!")}})
        )

        plan = planner.plan(
            [_note(1, "Basic", Word=""), _note(2, "Kanji", Word=""), _note(3, "Basic")]
        )

        assert [(entry.note_type, entry.note_count) for entry in plan.skipped] == [
            ("Basic", 2),
            ("Kanji", 1),
        ]

    def test_a_note_type_configured_under_another_case_still_runs(self):
        planner = NoteTypePlanner(
            NoteTypeScope({"vocab": {"enable": True, "tasks": _stamp("Word", "!")}})
        )

        plan = planner.plan([_note(1, "Vocab", Word="")])

        assert _updates(plan.notes[0]) == {"Word": "!"}

    def test_has_runnable_note_type_answers_before_any_note_is_read(self):
        assert not NoteTypePlanner(NoteTypeScope({})).has_runnable_note_type
        assert not NoteTypePlanner(
            NoteTypeScope({"Vocab": {"enable": False}})
        ).has_runnable_note_type
        assert NoteTypePlanner(
            NoteTypeScope({"Vocab": {"enable": True, "tasks": _stamp("Word", "!")}})
        ).has_runnable_note_type

    def test_a_task_section_this_version_cannot_parse_still_runs_the_note_type(self):
        planner = NoteTypePlanner(
            NoteTypeScope(
                {
                    "Vocab": {
                        "enable": True,
                        "tasks": {
                            _STAMP_ID: {
                                "enable": True,
                                "order": "whenever",
                                "field": "Word",
                                "stamp": "kept",
                            }
                        },
                    }
                }
            )
        )

        plan = planner.plan([_note(1, "Vocab", Word="")])

        # The unreadable ``order`` reverts; the readable options (and the note type) survive.
        assert _updates(plan.notes[0]) == {"Word": "kept"}
