"""Tests for the note-maintenance apply step (the write, against a fake collection)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.apply import ChangeApplier
from omnia.plugins.note_maintenance.runner import ChangePlan, FieldChange, NoteChange


class _FakeNote(dict):
    """A note that behaves like Anki's: ``keys()`` lists its fields, ``[]`` writes one."""


class _FakeCollection:
    """Records the notes handed to ``update_notes``."""

    def __init__(self, notes: dict[int, _FakeNote]) -> None:
        self._notes = notes
        self.updated: list[_FakeNote] = []

    def get_note(self, note_id: int) -> _FakeNote:
        return self._notes[note_id]

    def update_notes(self, notes: list[_FakeNote]) -> str:
        self.updated = list(notes)
        return "op-changes"


def _plan(*changes: NoteChange) -> ChangePlan:
    return ChangePlan(changes)


class TestChangeApplier:
    def test_writes_every_planned_field(self):
        col = _FakeCollection({1: _FakeNote(Word="old", Meaning="keep")})
        plan = _plan(NoteChange(1, (FieldChange("Word", "old", "new"),)))

        assert ChangeApplier(plan).write(col) == "op-changes"
        assert col.updated == [{"Word": "new", "Meaning": "keep"}]

    def test_skips_a_field_the_note_no_longer_has(self):
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(
            NoteChange(
                1,
                (
                    FieldChange("Word", "old", "new"),
                    FieldChange("Gone", "", "value"),
                ),
            )
        )

        ChangeApplier(plan).write(col)
        assert col.updated == [{"Word": "new"}]

    def test_a_note_whose_every_field_was_skipped_is_not_submitted(self):
        # Submitting it would bump its mod/usn and mark it modified for the next sync.
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(NoteChange(1, (FieldChange("Gone", "", "value"),)))

        ChangeApplier(plan).write(col)
        assert col.updated == []

    def test_an_empty_plan_writes_nothing_and_reports_zero(self):
        done: list[int] = []
        ChangeApplier(ChangePlan()).run(parent=None, on_done=done.append)
        assert done == [0]
