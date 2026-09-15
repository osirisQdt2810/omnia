"""Tests for what the model is asked.

The prompt is the feature — everything else in this plugin renders or remembers. So these are
not "does it contain the word grammar"; they are the four instructions that separate a useful
correction from a useless one, each of which the obvious prompt gets wrong.
"""

from __future__ import annotations

import json

import pytest

from omnia.plugins.phrase_check.correction import NATURALNESS, SPOKEN, WRITTEN
from omnia.plugins.phrase_check.prompt import build, language_name


class TestWhatItInsistsOn:
    def test_it_asks_for_separate_fixes_not_one_rewritten_blob(self):
        # Asked plainly for "the corrected sentence" a model returns one, and the reader learns
        # nothing. The list is what makes the panel worth reading twice.
        prompt = build("I have went.")

        assert "One entry per change" in prompt
        assert "Never bundle two unrelated problems" in prompt

    def test_it_asks_about_naturalness_as_its_own_category(self):
        # Grammatically perfect sentences no native speaker would say are what a learner most
        # needs flagged, and a model asked only about "errors" passes them.
        prompt = build("I have went.")

        assert NATURALNESS in prompt
        assert "no fluent speaker would actually say" in prompt

    def test_it_tells_the_model_to_leave_correct_text_alone(self):
        # Models rewrite for style unasked, and a panel that always finds five things teaches the
        # reader to ignore it.
        prompt = build("I went to the shop.")

        assert "Change nothing that is already correct" in prompt
        assert (
            "Inventing changes to look useful is worse than finding nothing" in prompt
        )

    def test_before_must_be_quoted_from_the_phrase(self):
        # The panel finds each fix inside the original to show it in place; a paraphrased
        # "before" cannot be found and the card renders against nothing.
        assert "copied EXACTLY from the phrase" in build("x")


class TestTheRegisterChangesTheStandard:
    def test_spoken_and_written_ask_different_questions(self):
        # "I ain't got none" is a mistake in writing and ordinary in speech. A corrector with one
        # standard is wrong half the time with total confidence.
        assert build("x", mode=SPOKEN) != build("x", mode=WRITTEN)

    def test_spoken_protects_contractions_rather_than_correcting_them(self):
        prompt = build("x", mode=SPOKEN)

        assert "Contractions" in prompt
        assert "not errors" in prompt

    def test_written_does_not_ask_for_ornate_language(self):
        # "Formal" alone gets legalese.
        prompt = build("x", mode=WRITTEN)

        assert "do not make it ornate or academic" in prompt

    def test_an_unknown_mode_falls_back_to_written(self):
        assert build("x", mode="shouted") == build("x", mode=WRITTEN)


class TestTheAnswerShape:
    def test_it_shows_a_filled_in_example_rather_than_a_schema(self):
        # A model handed an example matches it far more reliably than one handed a description.
        prompt = build("x")
        start = prompt.index("{")
        end = prompt.rindex("}") + 1

        shape = json.loads(prompt[start:end])
        assert set(shape) == {"already_good", "rewritten", "fixes"}
        assert set(shape["fixes"][0]) == {"before", "after", "kind", "why"}

    def test_it_names_the_kinds_the_parser_understands(self):
        from omnia.plugins.phrase_check.correction import (
            GRAMMAR,
            PUNCTUATION,
            SPELLING,
            WORD_CHOICE,
        )

        prompt = build("x")
        for kind in (GRAMMAR, WORD_CHOICE, NATURALNESS, SPELLING, PUNCTUATION):
            assert kind in prompt

    def test_a_phrase_that_is_fine_has_a_way_to_say_so(self):
        # Without one, a model with nothing to report invents something.
        assert "already_good" in build("x")


class TestTheExplanationLanguage:
    def test_it_asks_for_the_explanations_in_the_chosen_language(self):
        assert "Write every 'why' in Vietnamese" in build("x", language="Vietnamese")

    def test_the_phrase_itself_stays_english(self):
        # The language setting is about the reader, not about what is being corrected.
        assert "Keep the phrase itself in English" in build("x", language="Vietnamese")

    @pytest.mark.parametrize(
        "code,expected",
        [
            ("vi", "Vietnamese"),
            ("en", "English"),
            ("VI", "Vietnamese"),
            ("vi-VN", "Vietnamese"),
        ],
    )
    def test_a_code_becomes_a_name_a_model_understands(self, code, expected):
        assert language_name(code) == expected

    def test_nothing_chosen_means_english(self):
        # English is the one answer always readable by someone who chose to study in it.
        assert language_name(None) == "English"
        assert language_name("") == "English"

    def test_an_unrecognised_name_is_passed_through(self):
        # A model understands "Bahasa Indonesia" perfectly well; a table of every code is one
        # nobody maintains.
        assert language_name("Bahasa Indonesia") == "Bahasa Indonesia"


class TestThePhraseItself:
    def test_it_ends_with_the_phrase_so_nothing_follows_it(self):
        # Instructions after the input are what a long phrase pushes out of attention.
        assert (
            build("I have went to the shop.")
            .rstrip()
            .endswith("I have went to the shop.")
        )

    def test_surrounding_whitespace_from_a_web_selection_is_trimmed(self):
        assert build("  \n I have went. \n ").rstrip().endswith("I have went.")


class TestTheOrderIsAskedFor:
    """The panel shows the first few, so the ORDER is what a reader in a hurry sees.

    Left alone a model returns fixes in the order they appear in the sentence, which puts a
    stray comma above a wrong tense whenever the comma came first. Asking is the only lever
    there is — nothing downstream can re-rank what it cannot judge.
    """

    def test_it_asks_for_the_most_important_first(self):
        prompt = build("I have went.")

        assert "Order 'fixes'" in prompt
        assert "first" in prompt

    def test_it_says_the_order_does_not_limit_the_rewrite(self):
        # The trap: a model told "only the first few are shown" helpfully corrects only those.
        # The rewrite has to fix everything found, or the sentence handed back is still wrong
        # in the ways nobody had room to explain.
        prompt = build("I have went.")

        assert "must still fix EVERYTHING" in prompt

    def test_both_registers_carry_it(self):
        for mode in (SPOKEN, WRITTEN):
            assert "Order 'fixes'" in build("x", mode=mode), mode
