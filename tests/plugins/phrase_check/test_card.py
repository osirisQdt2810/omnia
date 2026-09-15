"""The note type a saved correction becomes, and the card it renders as.

All of it is string building, which is exactly why it is tested here: a card template is markup
nobody reads until it is wrong on somebody's screen, halfway through a review, in a note type
that already has a thousand notes in it.
"""

from __future__ import annotations

import re

import pytest

from omnia.plugins.phrase_check.card import (
    FIELD_CORRECTED,
    FIELD_FIXES,
    FIELD_PHRASE,
    FIELD_REGISTER,
    FIELDS,
    TWO_COLUMN_FROM,
    back_template,
    card_css,
    corrected_html,
    fixes_html,
    front_template,
    note_fields,
)
from omnia.plugins.phrase_check.correction import WRITTEN, parse


class _Fix:
    def __init__(
        self, before="a", after="b", why="w", kind="grammar", is_deletion=False
    ):
        self.before = before
        self.after = after
        self.why = why
        self.kind = kind
        self.is_deletion = is_deletion


def _correction(fixes=None, **kw):
    payload = {
        "rewritten": kw.pop("rewritten", "I went to the shop."),
        "fixes": (
            fixes
            if fixes is not None
            else [
                {
                    "before": "have went",
                    "after": "went",
                    "why": "Simple past.",
                    "kind": "grammar",
                }
            ]
        ),
    }
    return parse(
        payload, original=kw.pop("original", "I have went to the shop."), mode=WRITTEN
    )


def _text(html: str) -> str:
    return re.sub(r"<[^>]*>", "", html)


class TestTheFixList:
    def test_each_fix_is_its_own_item(self):
        html = fixes_html([_Fix(), _Fix(), _Fix()])

        assert html.count('class="pc-fix"') == 3

    def test_a_change_reads_before_then_after(self):
        html = fixes_html([_Fix(before="have went", after="went")])

        assert _text(html).index("have went") < _text(html).index("went")

    def test_a_deletion_says_the_words_went(self):
        html = fixes_html([_Fix(before="very", after="", is_deletion=True)])

        assert "pc-gone" in html
        assert "removed" in _text(html)

    def test_a_fix_with_no_reason_gets_no_empty_block(self):
        html = fixes_html([_Fix(why="   ")])

        assert "pc-why" not in html

    def test_a_fix_with_no_kind_gets_no_empty_label(self):
        html = fixes_html([_Fix(kind="")])

        assert "pc-kind" not in html

    def test_nothing_to_show_is_an_empty_field(self):
        # So the template's {{#Fixes}} section disappears rather than leaving a heading over
        # nothing. An empty <ul> would still take vertical room on a card that is short of it.
        assert fixes_html([]) == ""

    def test_a_short_list_stays_one_column(self):
        html = fixes_html([_Fix()] * (TWO_COLUMN_FROM - 1))

        assert "pc-two" not in html

    def test_a_longer_list_splits_into_two(self):
        # The request: many errors split vertically into two sides so the answer still fits.
        html = fixes_html([_Fix()] * TWO_COLUMN_FROM)

        assert "pc-two" in html

    def test_the_split_is_decided_here_not_in_css(self):
        # CSS cannot count. A media query knows the screen and a container query knows the box;
        # neither knows whether there are two fixes or nine.
        assert "column-count" in card_css()
        assert ".pc-fixes.pc-two" in card_css()


class TestTheCorrectedSentence:
    def test_the_changed_words_are_marked(self):
        html = corrected_html([("I ", False), ("went", True), (" home.", False)])

        assert '<mark class="pc-new">went</mark>' in html
        assert _text(html) == "I went home."

    def test_an_unmarked_sentence_has_no_marks(self):
        assert "<mark" not in corrected_html([("I went home.", False)])

    def test_a_run_that_is_not_one_is_skipped(self):
        html = corrected_html([("I ", False), "nope", ("went.", True)])

        assert _text(html) == "I went."

    def test_nothing_renders_as_nothing(self):
        assert corrected_html([]) == ""
        assert corrected_html(None) == ""


class TestEscaping:
    """A fix's words come from a model; the phrase is whatever was selected in a browser.

    Both end up in a note field, and a note field is rendered as HTML for ever after — in the
    reviewer, in the browser's preview, and in whatever the note is exported to.
    """

    def test_markup_in_a_fix_cannot_reach_the_card_as_markup(self):
        html = fixes_html(
            [
                _Fix(
                    before="<script>x</script>",
                    after='"><b>b</b>',
                    why="it is <i>wrong</i>",
                )
            ]
        )

        assert "<script" not in html
        assert "<b>b</b>" not in html
        assert "<i>wrong</i>" not in html
        assert "&lt;script" in html

    def test_markup_in_the_phrase_cannot_either(self):
        fields = note_fields(_correction(original="<img src=x onerror=alert(1)>"))

        assert "<img" not in fields[FIELD_PHRASE]
        assert "&lt;img" in fields[FIELD_PHRASE]

    def test_markup_in_the_rewrite_cannot_either(self):
        html = corrected_html([("<img src=x>", True)])

        assert "<img" not in html
        assert "&lt;img" in html


class TestTheNoteFields:
    def test_every_declared_field_is_filled(self):
        fields = note_fields(_correction())

        assert set(fields) == set(FIELDS)

    def test_the_phrase_is_what_was_written_not_what_it_should_be(self):
        # The front is the mistake. A card whose front already showed the correction is a card
        # you cannot fail, which is a card that teaches nothing.
        fields = note_fields(_correction())

        assert fields[FIELD_PHRASE] == "I have went to the shop."

    def test_the_corrected_field_carries_the_marks(self):
        # A SUBSTITUTION, not the default fixture: that one only deletes a word ("have went" ->
        # "went"), and a deletion leaves nothing in the rewrite to mark — correctly, since there
        # is no new text there to point at.
        fields = note_fields(
            _correction(
                fixes=[
                    {
                        "before": "for buy",
                        "after": "to buy",
                        "why": "w",
                        "kind": "grammar",
                    }
                ],
                original="I went to the shop for buy milk.",
                rewritten="I went to the shop to buy milk.",
            )
        )

        assert "<mark" in fields[FIELD_CORRECTED]
        assert _text(fields[FIELD_CORRECTED]) == "I went to the shop to buy milk."

    def test_a_deletion_leaves_nothing_to_mark(self):
        # The default fixture: "have went" -> "went" removes a word, so the rewrite has no new
        # text in it. Marking something anyway would be pointing at words nobody changed.
        fields = note_fields(_correction())

        assert "<mark" not in fields[FIELD_CORRECTED]
        assert _text(fields[FIELD_CORRECTED]) == "I went to the shop."

    def test_the_register_travels(self):
        assert note_fields(_correction())[FIELD_REGISTER] == WRITTEN

    def test_an_approved_phrase_still_produces_a_note(self):
        # Nothing was wrong, so there are no fixes — but the card is still worth keeping, and an
        # empty Fixes field is what makes the template's section disappear cleanly.
        fields = note_fields(
            _correction(fixes=[], rewritten="I went.", original="I went.")
        )

        assert fields[FIELD_FIXES] == ""
        assert _text(fields[FIELD_CORRECTED]) == "I went."


class TestTheTemplates:
    def test_the_front_asks_without_answering(self):
        front = front_template()

        assert "{{" + FIELD_PHRASE + "}}" in front
        for giveaway in (FIELD_CORRECTED, FIELD_FIXES):
            assert giveaway not in front, f"the question side shows {giveaway}"

    def test_the_back_shows_the_fixes_and_then_the_correction(self):
        back = back_template()

        assert back.index("{{" + FIELD_FIXES + "}}") < back.index(
            "{{" + FIELD_CORRECTED + "}}"
        )

    def test_the_back_does_not_reuse_the_front_side(self):
        # {{FrontSide}} would repeat "What is wrong with this?" above the answer, where the
        # question is no longer being asked, and spend the vertical room the fixes need.
        assert "FrontSide" not in back_template()

    def test_the_fixes_section_is_conditional(self):
        # So a phrase that was already correct does not get a heading over an empty list.
        back = back_template()

        assert "{{#" + FIELD_FIXES + "}}" in back
        assert "{{/" + FIELD_FIXES + "}}" in back

    @pytest.mark.parametrize("field", FIELDS)
    def test_every_field_is_referenced_by_a_template(self, field):
        # A field no template shows is a field nobody can see, which is a field that should not
        # be there — and is usually the sign of a rename that only went half way.
        both = front_template() + back_template()

        assert "{{" + field + "}}" in both, field

    def test_the_templates_only_reference_fields_that_exist(self):
        both = front_template() + back_template()
        referenced = set(re.findall(r"\{\{#?/?([A-Za-z][A-Za-z ]*)\}\}", both))

        assert referenced <= set(FIELDS), referenced - set(FIELDS)


class TestTheStylesheet:
    def test_it_styles_every_class_the_markup_emits(self):
        css = card_css()
        markup = (
            front_template()
            + back_template()
            + fixes_html([_Fix(), _Fix(), _Fix(), _Fix()])
            + corrected_html([("a", True)])
        )
        for cls in set(re.findall(r'class="([^"]+)"', markup)):
            for one in cls.split():
                assert "." + one in css, one

    def test_night_mode_is_handled_for_every_colour_rule(self):
        # Anki adds .night_mode to the body. A card that styles only the light case is unreadable
        # in the dark one, which is where a lot of people review.
        assert card_css().count(".night_mode") >= 8

    def test_it_holds_still(self):
        # A review is a still image. Motion during the one second someone spends deciding
        # whether they knew the answer is noise.
        css = card_css()

        assert "animation" not in css
        assert "transition" not in css

    def test_a_fix_is_not_split_across_the_column_gap(self):
        # The failure that makes columns look broken rather than tidy.
        assert "break-inside: avoid" in card_css()
