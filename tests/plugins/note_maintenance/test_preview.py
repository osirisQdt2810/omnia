"""Tests for the preview selection model (what the diff dialog shows and what Apply writes).

The dialog itself is Qt glue (no Qt headless), but everything it decides — the rows it builds,
which changes stay ticked, and the plan Apply hands to the writer — lives in the pure
:mod:`~omnia.plugins.note_maintenance.preview` model and is exercised here. The last class runs
the selected plan through the REAL applier against a fake collection, so "only the included
changes are submitted" is proven end to end.
"""

from __future__ import annotations

from omnia.plugins.note_maintenance.apply import ChangeApplier
from omnia.plugins.note_maintenance.preview import PreviewModel
from omnia.plugins.note_maintenance.runner import ChangePlan, FieldChange, NoteChange


class _FakeNote(dict):
    """A note that behaves like Anki's: ``keys()`` lists its fields, ``[]`` writes one."""


class _FakeCollection:
    """Records the notes handed to ``update_notes``."""

    def __init__(self, notes: dict[int, _FakeNote]) -> None:
        self._notes = notes
        self.updated: list[_FakeNote] = []

    def get_note(self, note_id: int) -> _FakeNote:
        from anki.errors import NotFoundError

        if note_id not in self._notes:
            raise NotFoundError(f"no such note: {note_id}")
        return self._notes[note_id]

    def update_notes(self, notes: list[_FakeNote]) -> str:
        self.updated = list(notes)
        return "op-changes"


def _plan() -> ChangePlan:
    """Two notes: the first with two changed fields, the second with one."""
    return ChangePlan(
        (
            NoteChange(
                1,
                (
                    FieldChange("Word", "old word", "new word"),
                    FieldChange("Synonyms", "a (x), b (y)", "a, b"),
                ),
            ),
            NoteChange(2, (FieldChange("Word", "keep", "changed"),)),
        )
    )


class TestPreviewModelConstruction:
    def test_builds_one_group_per_note_with_a_row_per_changed_field(self):
        model = PreviewModel(_plan())

        assert [note.note_id for note in model.notes] == [1, 2]
        assert [row.field for row in model.notes[0].rows] == ["Word", "Synonyms"]
        assert [row.field for row in model.notes[1].rows] == ["Word"]

    def test_rows_carry_the_marked_up_diff(self):
        model = PreviewModel(_plan())

        row = model.notes[1].rows[0]
        assert row.before_html == "<del>keep</del>"
        assert row.after_html == "<ins>changed</ins>"

    def test_drops_a_note_whose_changes_are_all_no_ops(self):
        # A field whose before == after renders no row, so the note has nothing to review.
        plan = ChangePlan((NoteChange(1, (FieldChange("Word", "same", "same"),)),))

        model = PreviewModel(plan)

        assert model.notes == ()
        assert model.is_empty is True

    def test_everything_starts_included(self):
        model = PreviewModel(_plan())

        assert model.selected_note_count == 2
        assert model.selected_field_count == 3
        assert model.selected_plan() == _plan()


class TestPreviewModelInclusion:
    def test_unticking_a_field_drops_only_that_field(self):
        model = PreviewModel(_plan())

        model.notes[0].set_included("Synonyms", False)

        assert model.selected_field_count == 2
        assert model.selected_plan() == ChangePlan(
            (
                NoteChange(1, (FieldChange("Word", "old word", "new word"),)),
                NoteChange(2, (FieldChange("Word", "keep", "changed"),)),
            )
        )

    def test_unticking_every_field_drops_the_whole_note(self):
        model = PreviewModel(_plan())

        model.notes[0].include_all(False)

        assert model.selected_note_count == 1
        assert model.selected_field_count == 1
        assert [note.note_id for note in model.selected_plan()] == [2]

    def test_the_note_tick_governs_all_its_fields(self):
        note = PreviewModel(_plan()).notes[0]

        note.include_all(False)
        assert note.included_fields == ()

        note.include_all(True)
        assert note.included_fields == ("Word", "Synonyms")

    def test_a_note_reports_when_it_is_only_partly_included(self):
        note = PreviewModel(_plan()).notes[0]
        assert (note.is_fully_included, note.is_partly_included) == (True, False)

        note.set_included("Word", False)
        assert (note.is_fully_included, note.is_partly_included) == (False, True)

        note.set_included("Synonyms", False)
        assert (note.is_fully_included, note.is_partly_included) == (False, False)

    def test_re_ticking_a_field_restores_it(self):
        note = PreviewModel(_plan()).notes[0]

        note.set_included("Word", False)
        note.set_included("Word", True)

        assert note.included_fields == ("Word", "Synonyms")

    def test_an_all_unticked_plan_is_empty(self):
        model = PreviewModel(_plan())

        for note in model:
            note.include_all(False)

        assert model.selected_plan().is_empty is True


class TestApplyWritesOnlyIncludedChanges:
    """The whole point of the preview: an unticked change must never reach the collection."""

    def test_only_the_ticked_fields_are_submitted(self):
        col = _FakeCollection(
            {
                1: _FakeNote(Word="old word", Synonyms="a (x), b (y)"),
                2: _FakeNote(Word="keep"),
            }
        )
        model = PreviewModel(_plan())
        model.notes[0].set_included("Synonyms", False)  # user keeps the synonym list
        model.notes[1].include_all(False)  # user skips the second note entirely

        ChangeApplier(model.selected_plan()).write(col)

        assert col.updated == [{"Word": "new word", "Synonyms": "a (x), b (y)"}]
