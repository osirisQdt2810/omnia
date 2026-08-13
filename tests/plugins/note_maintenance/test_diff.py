"""Tests for the note-maintenance inline HTML diff (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.diff import DiffRow, NoteDiff
from omnia.plugins.note_maintenance.runner import FieldChange, NoteChange


class TestDiffRow:
    def test_unchanged_field_produces_no_row(self):
        change = FieldChange(field="Word", before="same", after="same")
        assert DiffRow.build(change) is None

    def test_marks_the_replaced_words(self):
        row = DiffRow.build(FieldChange("Word", "a quick fox", "a slow fox"))
        assert row is not None
        assert row.before_html == "a <del>quick</del> fox"
        assert row.after_html == "a <ins>slow</ins> fox"

    def test_marks_an_insertion_only_on_the_after_side(self):
        row = DiffRow.build(FieldChange("Word", "a fox", "a red fox"))
        assert row is not None
        assert row.before_html == "a fox"
        assert row.after_html == "a <ins>red </ins>fox"

    def test_marks_a_deletion_only_on_the_before_side(self):
        row = DiffRow.build(FieldChange("Word", "a red fox", "a fox"))
        assert row is not None
        assert row.before_html == "a <del>red </del>fox"
        assert row.after_html == "a fox"

    def test_the_fields_own_markup_is_escaped_not_rendered(self):
        row = DiffRow.build(FieldChange("Word", "<b>bold</b>", "plain"))
        assert row is not None
        assert row.before_html == "<del>&lt;b&gt;bold&lt;/b&gt;</del>"
        assert row.after_html == "<ins>plain</ins>"

    def test_keeps_the_field_name(self):
        row = DiffRow.build(FieldChange("Synonyms", "a", "b"))
        assert row is not None and row.field == "Synonyms"


class TestNoteDiff:
    def test_rows_skip_unchanged_fields(self):
        change = NoteChange(
            note_id=4,
            fields=(
                FieldChange("Word", "same", "same"),
                FieldChange("Meaning", "old", "new"),
            ),
        )
        diff = NoteDiff(change)
        assert diff.note_id == 4
        assert [row.field for row in diff.rows()] == ["Meaning"]

    def test_no_rows_when_nothing_changed(self):
        change = NoteChange(note_id=4, fields=(FieldChange("Word", "x", "x"),))
        assert NoteDiff(change).rows() == []
