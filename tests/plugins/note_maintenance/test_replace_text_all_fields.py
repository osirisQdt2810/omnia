"""Tests for the replace_text_all_fields maintenance task (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.tasks.replace_text_all_fields import (
    ReplaceTextAllFieldsConfig,
    ReplaceTextAllFieldsTask,
)


def _note(**fields: str) -> NoteView:
    return NoteView(note_id=5, note_type="Vocab", fields=dict(fields))


class TestReplaceTextAllFieldsTask:
    def test_replaces_in_every_field_that_matches(self):
        task = ReplaceTextAllFieldsTask(
            ReplaceTextAllFieldsConfig(find="promo", replace="")
        )
        note = _note(Word="promo word", Meaning="a meaning", Notes="promo promo")
        assert task.process(note) == {"Word": " word", "Notes": " "}

    def test_empty_find_is_a_no_op(self):
        task = ReplaceTextAllFieldsTask(ReplaceTextAllFieldsConfig())
        assert task.process(_note(Word="anything")) == {}

    def test_replaces_with_the_configured_text(self):
        task = ReplaceTextAllFieldsTask(
            ReplaceTextAllFieldsConfig(find="&nbsp;", replace=" ")
        )
        assert task.process(_note(Word="a&nbsp;b")) == {"Word": "a b"}

    def test_match_is_case_sensitive(self):
        task = ReplaceTextAllFieldsTask(
            ReplaceTextAllFieldsConfig(find="Promo", replace="")
        )
        assert task.process(_note(Word="promo")) == {}

    def test_no_change_when_nothing_matches(self):
        task = ReplaceTextAllFieldsTask(
            ReplaceTextAllFieldsConfig(find="promo", replace="")
        )
        assert task.process(_note(Word="clean", Meaning="clean")) == {}
