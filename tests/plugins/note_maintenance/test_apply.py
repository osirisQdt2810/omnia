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
        # Anki raises NotFoundError for a note that no longer exists — so does the fake.
        from anki.errors import NotFoundError

        if note_id not in self._notes:
            raise NotFoundError(f"no such note: {note_id}")
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


class TestChangeApplierMissingNotes:
    """A note deleted between the preview and the apply must not cost the whole batch."""

    def test_skips_a_deleted_note_and_writes_the_rest(self):
        col = _FakeCollection({1: _FakeNote(Word="old"), 3: _FakeNote(Word="old")})
        plan = _plan(
            NoteChange(1, (FieldChange("Word", "old", "new"),)),
            NoteChange(2, (FieldChange("Word", "old", "new"),)),  # deleted meanwhile
            NoteChange(3, (FieldChange("Word", "old", "third"),)),
        )
        applier = ChangeApplier(plan)

        assert applier.write(col) == "op-changes"
        assert col.updated == [{"Word": "new"}, {"Word": "third"}]

    def test_records_the_missing_note_ids(self):
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(
            NoteChange(1, (FieldChange("Word", "old", "new"),)),
            NoteChange(2, (FieldChange("Word", "old", "new"),)),
        )
        applier = ChangeApplier(plan)
        assert applier.missing_note_ids == ()

        applier.write(col)
        assert applier.missing_note_ids == (2,)

    def test_reports_only_the_notes_actually_written(self):
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(
            NoteChange(1, (FieldChange("Word", "old", "new"),)),
            NoteChange(2, (FieldChange("Word", "old", "new"),)),
        )
        applier = ChangeApplier(plan)
        applier.write(col)

        assert applier.written_note_count == 1
