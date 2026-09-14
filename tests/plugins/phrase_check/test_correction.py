"""Tests for what a correction is, before anything renders it.

The shape is the feature. "Your sentence should be X" teaches nothing, and one paragraph
explaining six unrelated problems is read by nobody — so an answer is a LIST of small separate
fixes, each with its own reason, plus the whole phrase rewritten.

Two properties carry the rest, and both fail in the direction that looks fine:

* a fix that changes nothing is noise wearing a confident label, and is dropped here rather than
  in the page, so every surface agrees;
* the highlighting has to join back into exactly the rewrite, or the reader is shown a sentence
  nobody wrote.
"""

from __future__ import annotations

import pytest

from omnia.plugins.phrase_check.correction import (
    SPOKEN,
    WRITTEN,
    Correction,
    CorrectionError,
    Fix,
    highlight,
    parse,
)


class TestReadingAModelsAnswer:
    def _payload(self, **over):
        payload = {
            "rewritten": "I have gone to the shop.",
            "fixes": [
                {
                    "before": "have went",
                    "after": "have gone",
                    "why": "The past participle of 'go' is 'gone'.",
                    "kind": "grammar",
                }
            ],
        }
        payload.update(over)
        return payload

    def test_it_reads_the_rewrite_and_the_fixes(self):
        correction = parse(self._payload(), original="I have went to the shop.")

        assert correction.rewritten == "I have gone to the shop."
        assert len(correction.fixes) == 1
        assert correction.fixes[0].why.startswith("The past participle")

    def test_the_original_comes_from_the_REQUEST_not_the_answer(self):
        # A model that paraphrases the input in its echo would otherwise make the highlighting
        # diff against a sentence nobody wrote.
        correction = parse(
            self._payload(original="something the model made up"),
            original="I have went to the shop.",
        )

        assert correction.original == "I have went to the shop."

    def test_a_fix_that_changes_nothing_is_dropped(self):
        # Models return these: identical text, confident explanation. A card reading
        # "nothing -> nothing" is one the reader has to work out is noise.
        correction = parse(
            self._payload(
                fixes=[
                    {"before": "the  cat", "after": "the cat", "why": "spacing"},
                    {"before": "have went", "after": "have gone", "why": "tense"},
                ]
            ),
            original="I have went to the shop.",
        )

        assert [fix.before for fix in correction.fixes] == ["have went"]

    def test_an_answer_with_no_rewrite_is_refused(self):
        with pytest.raises(CorrectionError, match="without a corrected version"):
            parse({"fixes": []}, original="anything")

    def test_something_that_is_not_a_correction_says_so(self):
        for payload in ("<html>", [], None, 12):
            with pytest.raises(
                CorrectionError, match="did not answer with a correction"
            ):
                parse(payload, original="anything")

    def test_a_phrase_with_nothing_wrong_is_a_real_answer(self):
        # Distinct from a broken one: it arrives WITH the original echoed back.
        correction = parse(
            {"rewritten": "I went to the shop.", "fixes": [], "already_good": True},
            original="I went to the shop.",
        )

        assert correction.already_good is True
        assert correction.changed is False

    def test_an_unknown_mode_falls_back_to_written(self):
        assert parse(self._payload(), original="x", mode="shouted").mode == WRITTEN

    def test_the_mode_survives_when_it_is_one_we_know(self):
        assert parse(self._payload(), original="x", mode=SPOKEN).mode == SPOKEN

    def test_an_explanation_under_either_key_is_read(self):
        # Models are inconsistent about this one and the explanation is the whole point of a fix.
        correction = parse(
            self._payload(
                fixes=[{"before": "a", "after": "b", "explanation": "because"}]
            ),
            original="a",
        )

        assert correction.fixes[0].why == "because"


class TestAFix:
    def test_an_empty_replacement_is_a_deletion(self):
        assert Fix(before="very", after="").is_deletion is True
        assert Fix(before="very", after="   ").is_deletion is True

    def test_a_replacement_is_not(self):
        assert Fix(before="very", after="extremely").is_deletion is False

    def test_whitespace_alone_is_not_a_change(self):
        assert Fix(before="the  cat", after="the cat").changes_anything is False

    def test_case_alone_IS_a_change(self):
        # Missing capitals on "I" and at the start of a sentence are among the commonest
        # written-English learner errors, and `written` is the register this ships in. Folding
        # case made every one of those fixes invisible: filtered out as noise, `already_good`
        # True, and the panel telling the user the sentence was fine beside one that differed
        # from what they wrote.
        assert Fix(before="i", after="I").changes_anything is True
        assert Fix(before="the cat sat", after="The cat sat").changes_anything is True

    def test_it_is_a_change_in_the_other_direction_too(self):
        # This direction is what the old rule was written for — a model "fixing" a capital it
        # invented. But a rule that discards those also discards the ones that are right, and
        # being silent about a real mistake is the worse half of that trade.
        assert Fix(before="The cat", after="the cat").changes_anything is True

    def test_a_real_change_is_one(self):
        assert Fix(before="have went", after="have gone").changes_anything is True


class TestHighlightingWhatChanged:
    def test_the_runs_join_back_into_exactly_the_rewrite(self):
        # The property that matters most: anything else shows the reader a sentence nobody wrote.
        original = "I have went to the shop yesterday."
        rewritten = "I went to the shop yesterday."

        assert "".join(text for text, _ in highlight(original, rewritten)) == rewritten

    def test_only_the_changed_words_are_marked(self):
        runs = highlight("I have went to the shop.", "I have gone to the shop.")
        marked = "".join(text for text, is_new in runs if is_new)

        assert marked.strip() == "gone"

    def test_an_unchanged_phrase_marks_nothing(self):
        runs = highlight("I went to the shop.", "I went to the shop.")

        assert not any(is_new for _text, is_new in runs)

    def test_it_marks_whole_words_not_letters(self):
        # A character diff bolds two letters in the middle of a word, which at body-text size is
        # unreadable. A word is the smallest unit a reader can see has changed.
        runs = highlight("I recieve mail.", "I receive mail.")
        marked = "".join(text for text, is_new in runs if is_new)

        assert marked.strip() == "receive"

    def test_added_words_are_marked(self):
        runs = highlight("I went shop.", "I went to the shop.")
        marked = "".join(text for text, is_new in runs if is_new)

        assert "to the" in marked

    def test_a_removed_word_leaves_nothing_marked_but_still_joins(self):
        original, rewritten = "It is very very good.", "It is very good."
        runs = highlight(original, rewritten)

        assert "".join(text for text, _ in runs) == rewritten

    def test_neighbouring_runs_of_the_same_kind_are_merged(self):
        # Fewer spans for the page, and no two adjacent <b> tags that render as one but are two.
        # Two consecutive changed words must come back as ONE run, not two.
        runs = highlight("a b c d", "a X Y d")
        kinds = [is_new for _text, is_new in runs]

        assert kinds == [False, True, False], runs
        assert all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1))

    def test_punctuation_travels_with_its_word(self):
        # A fix that only adds a comma must still bold something; a bare "," on its own would be
        # invisible.
        runs = highlight("However I went", "However, I went")
        marked = "".join(text for text, is_new in runs if is_new)

        assert "However," in marked

    def test_an_empty_original_marks_the_whole_rewrite(self):
        runs = highlight("", "Something new.")

        assert all(is_new for _text, is_new in runs)


class TestTheCorrectionItself:
    def test_it_knows_whether_anything_changed(self):
        assert Correction(original="a b", rewritten="a c").changed is True
        assert Correction(original="a b", rewritten="a  b").changed is False

    def test_highlighting_is_available_from_the_correction(self):
        correction = Correction(original="I went shop", rewritten="I went to the shop")

        assert "".join(t for t, _ in correction.highlighted()) == "I went to the shop"


class TestAlreadyGoodMeansWhatItSays:
    """ "This is fine" and "it answered with nothing usable" are opposite answers.

    The panel renders them completely differently — one says "nothing to change", the other
    shows a rewrite — so conflating them puts a reassurance next to a different sentence.
    """

    def test_a_rewrite_with_every_fix_dropped_is_not_already_good(self):
        # The way this happens in practice: a model names its keys `original`/`corrected`
        # instead of `before`/`after`, so every fix parses to empty, changes nothing, and is
        # filtered out. The rewrite is still there and still different.
        correction = parse(
            {
                "rewritten": "I haven't got any money.",
                "fixes": [
                    {"original": "ain't got none", "corrected": "haven't got any"}
                ],
            },
            original="I ain't got none.",
            mode=WRITTEN,
        )

        assert correction.fixes == (), "the unparseable fix was kept"
        assert correction.changed is True
        assert (
            correction.already_good is False
        ), "it told the user the sentence was fine while showing a different one"

    def test_a_no_op_rewrite_with_no_fixes_is_already_good(self):
        # The case the fix filter exists for: the model echoed the sentence back and every
        # "fix" it listed changed nothing. That IS "this is fine".
        correction = parse(
            {
                "rewritten": "I have gone to the shop.",
                "fixes": [{"before": "gone", "after": "gone"}],
            },
            original="I have gone to the shop.",
            mode=WRITTEN,
        )

        assert correction.fixes == ()
        assert correction.already_good is True

    def test_the_model_saying_so_is_not_enough_on_its_own(self):
        # The flag is not consulted at all: a payload claiming `already_good` beside a rewrite
        # that differs is a reassurance contradicted by the sentence printed under it, and the
        # panel renders the two together. The sentences decide.
        correction = parse(
            {"rewritten": "I have gone.", "already_good": True, "fixes": []},
            original="I have went.",
            mode=WRITTEN,
        )

        assert correction.already_good is False
        assert correction.changed is True

    def test_an_echoed_sentence_is_already_good_whatever_the_flag_says(self):
        correction = parse(
            {"rewritten": "I have gone.", "fixes": []},
            original="I have gone.",
            mode=WRITTEN,
        )

        assert correction.already_good is True

    def test_whitespace_alone_is_not_a_correction(self):
        correction = parse(
            {"rewritten": "  I have   gone. ", "fixes": []},
            original="I have gone.",
            mode=WRITTEN,
        )

        assert correction.already_good is True, "respacing was reported as a rewrite"


class TestACapitalIsACorrection:
    """The bug this class exists for: a capitalisation fix, erased and then denied.

    `i went to school yesterday.` is corrected to `I went to school yesterday.` — a real fix, of
    one of the commonest written-English learner errors. Folding case made it invisible to the
    whole module: the fix was filtered out as noise, `already_good` came out True, and nothing
    was bolded, so the panel said "nothing to change" beside a sentence that was not the one the
    user wrote.
    """

    def test_a_capitalisation_fix_survives_parsing(self):
        correction = parse(
            {
                "rewritten": "I went to school yesterday.",
                "fixes": [
                    {
                        "before": "i",
                        "after": "I",
                        "kind": "punctuation",
                        "why": "The pronoun I is always capitalised.",
                    }
                ],
            },
            original="i went to school yesterday.",
            mode=WRITTEN,
        )

        assert len(correction.fixes) == 1, "the real fix was filtered out as noise"
        assert correction.already_good is False, "it said the sentence was fine"
        assert correction.changed is True

    def test_the_capitalised_word_is_the_one_marked(self):
        correction = parse(
            {"rewritten": "The cat sat.", "fixes": []},
            original="the cat sat.",
            mode=WRITTEN,
        )

        runs = correction.highlighted()

        assert "".join(text for text, _new in runs) == "The cat sat."
        assert any(is_new for _text, is_new in runs), "the fixed capital was not marked"
        # The runs carry their trailing whitespace, so they join back to the sentence exactly.
        assert [text.strip() for text, is_new in runs if is_new] == ["The"]

    def test_a_capitalisation_fix_inside_a_bigger_rewrite_is_still_marked(self):
        correction = parse(
            {"rewritten": "I have gone to school.", "fixes": []},
            original="i have went to school.",
            mode=WRITTEN,
        )

        marked = [text for text, is_new in correction.highlighted() if is_new]

        assert (
            "I " in "".join(marked) or "I" in marked
        ), f"the capital was not marked: {marked}"
        assert "gone" in "".join(marked)
