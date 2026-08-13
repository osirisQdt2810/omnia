"""Tests for the reformat_synonyms maintenance task (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.tasks.reformat_synonyms import (
    ReformatSynonymsConfig,
    ReformatSynonymsTask,
)


def _note(value: str) -> NoteView:
    return NoteView(note_id=3, note_type="Vocab", fields={"Synonyms": value})


class TestReformatSynonymsTask:
    def test_pairs_words_with_their_transcriptions(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig())
        note = _note("modest, meek (ˈmɒdɪst, miːk)")
        assert task.process(note) == {"Synonyms": "modest (ˈmɒdɪst), meek (miːk)"}

    def test_skips_when_counts_differ_and_strict(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig(strict_count_match=True))
        assert task.process(_note("modest, meek (ˈmɒdɪst)")) == {}

    def test_pairs_what_lines_up_when_not_strict(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig(strict_count_match=False))
        note = _note("modest, meek (ˈmɒdɪst)")
        assert task.process(note) == {"Synonyms": "modest (ˈmɒdɪst)"}

    def test_no_change_without_a_trailing_group(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig())
        assert task.process(_note("modest, meek")) == {}

    def test_no_change_when_the_field_is_empty(self):
        assert ReformatSynonymsTask(ReformatSynonymsConfig()).process(_note("")) == {}

    def test_no_change_when_already_paired(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig())
        assert task.process(_note("modest (ˈmɒdɪst)")) == {}

    def test_leaves_an_already_paired_list_alone_even_when_not_strict(self):
        # Regression: re-running the task must never re-bracket its own output.
        task = ReformatSynonymsTask(ReformatSynonymsConfig(strict_count_match=False))
        assert task.process(_note("modest (ˈmɒdɪst), meek (miːk)")) == {}

    def test_reads_the_configured_field(self):
        task = ReformatSynonymsTask(ReformatSynonymsConfig(field="Antonyms"))
        note = NoteView(note_id=3, fields={"Antonyms": "bold, brash (bəʊld, bræʃ)"})
        assert task.process(note) == {"Antonyms": "bold (bəʊld), brash (bræʃ)"}
