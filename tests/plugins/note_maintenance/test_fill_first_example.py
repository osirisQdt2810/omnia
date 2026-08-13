"""Tests for the fill_first_example maintenance task (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.tasks.fill_first_example import (
    FillFirstExampleConfig,
    FillFirstExampleTask,
)

_CONFIG = FillFirstExampleConfig(
    source_field="Clozed First Example",
    target_field="First Example",
)


def _note(clozed: str, plain: str = "") -> NoteView:
    return NoteView(
        note_id=11,
        note_type="Vocab",
        fields={"First Example": plain, "Clozed First Example": clozed},
    )


class TestFillFirstExampleTask:
    def test_fills_an_empty_target_with_the_cloze_unwrapped(self):
        # The target is the PLAIN twin: it must never receive raw {{c1::…}} markup.
        task = FillFirstExampleTask(_CONFIG)
        note = _note("She {{c1::plunged}} into the water.")
        assert task.process(note) == {"First Example": "She plunged into the water."}

    def test_strips_the_markup_it_copies(self):
        task = FillFirstExampleTask(_CONFIG)
        note = _note("She <b>{{c1::plunged::verb}}</b> into the&nbsp;water.")
        assert task.process(note) == {"First Example": "She plunged into the water."}

    def test_a_refilled_target_is_a_fixed_point(self):
        source = "She {{c1::plunged}} into the water."
        task = FillFirstExampleTask(_CONFIG)
        filled = task.process(_note(source))["First Example"]
        assert task.process(_note(source, filled)) == {}

    def test_no_change_when_the_source_carries_no_words(self):
        # Stripping "[sound:x.mp3]" leaves nothing — emptying the target is not a refill.
        task = FillFirstExampleTask(_CONFIG)
        assert task.process(_note("[sound:plunge.mp3]", "She swam.")) == {}

    def test_no_change_when_the_source_is_empty(self):
        assert FillFirstExampleTask(_CONFIG).process(_note("  ", "She swam.")) == {}

    def test_no_change_when_the_two_hold_the_same_words(self):
        # The cloze wrapper and the <b> markup are not a difference — only the words count.
        task = FillFirstExampleTask(_CONFIG)
        note = _note(
            "She {{c1::plunged}} into the <b>water</b>.", "She plunged into the water."
        )
        assert task.process(note) == {}

    def test_refills_when_the_sentences_drifted_apart(self):
        task = FillFirstExampleTask(_CONFIG)
        note = _note("She {{c1::plunged}} into the icy lake at dawn.", "He ran home.")
        assert task.process(note) == {
            "First Example": "She plunged into the icy lake at dawn."
        }

    def test_threshold_zero_never_refills(self):
        task = FillFirstExampleTask(_CONFIG.copy(update={"threshold": 0.0}))
        assert task.process(_note("She plunged in.", "He ran home.")) == {}

    def test_threshold_one_refills_unless_word_for_word_identical(self):
        task = FillFirstExampleTask(_CONFIG.copy(update={"threshold": 1.0}))
        note = _note("She plunged into the water!", "She plunged into the water")
        # Same words -> similarity 1.0 -> still left alone.
        assert task.process(note) == {}
        assert task.process(_note("She plunged in.", "She plunged into the water")) == {
            "First Example": "She plunged in."
        }

    def test_no_change_when_the_raw_values_are_identical(self):
        task = FillFirstExampleTask(_CONFIG.copy(update={"threshold": 1.0}))
        assert task.process(_note("She swam.", "She swam.")) == {}
