"""Tests for the strip_ipa maintenance task (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.tasks.strip_ipa import (
    StripIpaConfig,
    StripIpaTask,
)


def _note(**fields: str) -> NoteView:
    return NoteView(note_id=1, note_type="Vocab", fields=dict(fields))


class TestStripIpaTask:
    def test_strips_ipa_from_every_segment(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA"}))
        note = _note(Synonyms="modest (ˈmɒdɪst), meek (miːk)", SynonymsNoIPA="")
        assert task.process(note) == {"SynonymsNoIPA": "modest, meek"}

    def test_keeps_segments_that_are_not_annotated(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA"}))
        note = _note(Synonyms="modest (ˈmɒdɪst), meek", SynonymsNoIPA="")
        assert task.process(note) == {"SynonymsNoIPA": "modest, meek"}

    def test_no_change_when_nothing_is_annotated(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA"}))
        assert task.process(_note(Synonyms="modest, meek")) == {}

    def test_no_change_when_source_is_empty(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA"}))
        assert task.process(_note(Synonyms="   ")) == {}

    def test_empty_target_strips_the_source_in_place(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": ""}))
        note = _note(Synonyms="modest (ˈmɒdɪst)")
        assert task.process(note) == {"Synonyms": "modest"}

    def test_no_change_when_target_already_holds_the_result(self):
        task = StripIpaTask(StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA"}))
        note = _note(Synonyms="modest (ˈmɒdɪst)", SynonymsNoIPA="modest")
        assert task.process(note) == {}

    def test_handles_several_field_pairs(self):
        task = StripIpaTask(
            StripIpaConfig(fields={"Synonyms": "SynonymsNoIPA", "Antonyms": ""})
        )
        note = _note(Synonyms="meek (miːk)", Antonyms="bold (bəʊld)")
        assert task.process(note) == {"SynonymsNoIPA": "meek", "Antonyms": "bold"}
