"""Tests for the note-maintenance apply step (the write, against a fake collection)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.apply import ApplyOutcome, ChangeApplier
from omnia.plugins.note_maintenance.runner import ChangePlan, FieldChange, NoteChange


class _FakeNote(dict):
    """A note that behaves like Anki's: ``keys()`` lists its fields, ``[]`` writes one."""


class _FakeCollection:
    """Records the notes handed to ``update_notes``."""

    def __init__(self, notes: dict[int, _FakeNote]) -> None:
        self._notes = notes
        self.updated: list[_FakeNote] = []
        self.update_calls = 0

    def get_note(self, note_id: int) -> _FakeNote:
        # Anki raises NotFoundError for a note that no longer exists — so does the fake.
        from anki.errors import NotFoundError

        if note_id not in self._notes:
            raise NotFoundError(f"no such note: {note_id}")
        return self._notes[note_id]

    def update_notes(self, notes: list[_FakeNote]) -> str:
        self.update_calls += 1
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

    def test_a_batch_with_nothing_left_to_write_opens_no_undo_entry(self):
        # col.update_notes([]) still creates an undo step, so the user would get a Ctrl+Z that
        # puts nothing back. Nothing to write -> no write at all.
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(NoteChange(1, (FieldChange("Gone", "", "value"),)))

        ChangeApplier(plan).write(col)
        assert col.update_calls == 0

    def test_an_empty_plan_writes_nothing_and_reports_zero(self):
        done: list[ApplyOutcome] = []
        ChangeApplier(ChangePlan()).run(parent=None, on_done=done.append)
        assert done == [ApplyOutcome()]


class TestChangeApplierStaleNotes:
    """A note edited between the preview and the apply keeps the user's own text."""

    def test_leaves_a_field_that_changed_since_the_preview(self):
        col = _FakeCollection({1: _FakeNote(Word="edited by hand")})
        plan = _plan(NoteChange(1, (FieldChange("Word", "old", "new"),)))

        ChangeApplier(plan).write(col)
        assert col.updated == []

    def test_records_the_stale_note_ids(self):
        col = _FakeCollection({1: _FakeNote(Word="edited by hand")})
        plan = _plan(NoteChange(1, (FieldChange("Word", "old", "new"),)))
        applier = ChangeApplier(plan)
        assert applier.outcome.stale_note_ids == ()

        applier.write(col)
        assert applier.outcome.stale_note_ids == (1,)

    def test_the_notes_other_fields_are_still_written(self):
        col = _FakeCollection({1: _FakeNote(Word="edited by hand", Meaning="old")})
        plan = _plan(
            NoteChange(
                1,
                (
                    FieldChange("Word", "old", "new"),
                    FieldChange("Meaning", "old", "fresh"),
                ),
            )
        )

        ChangeApplier(plan).write(col)
        assert col.updated == [{"Word": "edited by hand", "Meaning": "fresh"}]


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
        assert applier.outcome.missing_note_ids == ()

        applier.write(col)
        assert applier.outcome.missing_note_ids == (2,)

    def test_reports_only_the_notes_actually_written(self):
        col = _FakeCollection({1: _FakeNote(Word="old")})
        plan = _plan(
            NoteChange(1, (FieldChange("Word", "old", "new"),)),
            NoteChange(2, (FieldChange("Word", "old", "new"),)),
        )
        applier = ChangeApplier(plan)
        applier.write(col)

        assert applier.outcome.written_note_count == 1


class TestApplyOutcomeMessage:
    """The outcome owns the wording, so a skipped note cannot be reported as a success."""

    def test_a_clean_write_reports_the_count_and_the_undo(self):
        message = ApplyOutcome(written_note_count=3).message

        assert message == "Omnia: 3 note(s) updated — Ctrl+Z undoes the batch."

    def test_a_write_of_nothing_promises_no_undo(self):
        # Nothing written means no undo entry was opened, so Ctrl+Z would put back whatever
        # the user did BEFORE this run.
        message = ApplyOutcome(missing_note_ids=(2,)).message

        assert message.startswith("Omnia: 0 note(s) updated.")
        assert "Ctrl+Z" not in message

    def test_skipped_notes_are_named_in_the_same_line(self):
        message = ApplyOutcome(
            written_note_count=1, missing_note_ids=(2,), stale_note_ids=(3, 4)
        ).message

        assert message.startswith("Omnia: 1 note(s) updated")
        assert "1 note(s) had been deleted" in message
        assert "2 note(s) changed since the preview" in message

    def test_the_write_hands_its_own_outcome_to_the_message(self):
        col = _FakeCollection({1: _FakeNote(Word="edited by hand")})
        plan = _plan(
            NoteChange(1, (FieldChange("Word", "old", "new"),)),
            NoteChange(2, (FieldChange("Word", "old", "new"),)),  # deleted meanwhile
        )
        applier = ChangeApplier(plan)
        applier.write(col)

        message = applier.outcome.message
        assert "0 note(s) updated" in message
        assert "1 note(s) had been deleted" in message
        assert "1 note(s) changed since the preview" in message
