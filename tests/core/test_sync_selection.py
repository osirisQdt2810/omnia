"""Tests for what the user has chosen, across all three parts of an offer.

The parts are not independent, which is why they are one object: choosing a deck lights up the
note types that deck's cards need, and dropping one of those changes what choosing the deck
means. The rule with the corners in it is here, and the page only paints what this answers —
the version where the page worked it out itself painted every sibling of a picked deck as
half-selected, and no test could see it.
"""

from __future__ import annotations

from omnia.core.sync import ConfigSummary, DeckEntry, Inventory, NoteTypeEntry
from omnia.core.sync.selection import (
    DROPPED,
    IDLE,
    NEEDED,
    PICKED,
    UNPICKED,
    OfferSelection,
)


def _offer() -> OfferSelection:
    return OfferSelection(
        Inventory(
            machine="mac-mini",
            decks=(
                DeckEntry(id=1, name="Japanese", cards=5, note_types=("Basic",)),
                DeckEntry(
                    id=2,
                    name="Japanese::Kanji",
                    cards=100,
                    note_types=("Cloze", "Vocab"),
                ),
                DeckEntry(id=3, name="English", cards=7, note_types=("Basic",)),
            ),
            note_types=(
                NoteTypeEntry(name="Basic", notes=12),
                NoteTypeEntry(name="Cloze", notes=100),
                NoteTypeEntry(name="Vocab", notes=40),
                NoteTypeEntry(name="Unused", notes=0),
            ),
            config=ConfigSummary(features=("smart_notes", "audio_speed")),
        )
    )


class TestNoteTypesFollowTheDecks:
    def test_nothing_is_lit_before_a_deck_is_picked(self):
        offer = _offer()

        assert offer.needed == set()
        assert offer.note_type_state("Cloze") == IDLE

    def test_picking_a_deck_lights_what_its_cards_need(self):
        offer = _offer()

        offer.pick_deck("Japanese::Kanji")

        assert offer.note_type_state("Cloze") == NEEDED
        assert offer.note_type_state("Vocab") == NEEDED

    def test_picking_a_parent_lights_what_its_whole_subtree_needs(self):
        offer = _offer()

        offer.pick_deck("Japanese")

        assert offer.needed == {"Basic", "Cloze", "Vocab"}

    def test_a_note_type_nothing_uses_stays_idle(self):
        offer = _offer()
        offer.pick_deck("Japanese")

        assert offer.note_type_state("Unused") == IDLE

    def test_unpicking_the_last_deck_that_needed_one_puts_it_out(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")

        offer.pick_deck("Japanese::Kanji")

        assert offer.note_type_state("Cloze") == IDLE

    def test_another_deck_still_needing_it_keeps_it_lit(self):
        offer = _offer()
        offer.pick_deck("Japanese")  # Basic, via the parent
        offer.pick_deck("English")  # Basic too

        offer.pick_deck("Japanese")

        assert offer.note_type_state("Basic") == NEEDED

    def test_picking_a_deck_answers_for_the_note_types_as_well(self):
        # One answer per click. A page that had to ask a second question would paint the two
        # halves a frame apart, and the tree is the half that redraws slowly.
        offer = _offer()

        answer = offer.pick_deck("Japanese::Kanji")

        assert answer["decks"]["Japanese::Kanji"] == "picked"
        assert answer["note_types"]["Cloze"] == NEEDED


class TestDroppingANoteType:
    def test_the_deck_still_comes_without_it(self):
        # The case this exists for: a deck holding two note types where only one is worth
        # carrying. Dropping one must not drop the deck.
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")

        offer.toggle_note_type("Vocab")

        assert "Japanese::Kanji" in offer.decks
        assert offer.note_types == {"Cloze"}
        assert offer.note_type_state("Vocab") == DROPPED

    def test_clicking_it_again_takes_it_back(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")
        offer.toggle_note_type("Vocab")

        offer.toggle_note_type("Vocab")

        assert offer.note_type_state("Vocab") == NEEDED

    def test_a_note_type_no_deck_needs_can_be_chosen_on_its_own(self):
        # Its definition travels with no notes — which is how a second machine is set up to
        # author the same kind of card before there is anything to put in it.
        offer = _offer()

        answer = offer.toggle_note_type("Unused")

        assert answer["note_types"]["Unused"] == PICKED
        assert offer.note_types == {"Unused"}

    def test_choosing_one_again_unchooses_it(self):
        offer = _offer()
        offer.toggle_note_type("Unused")

        offer.toggle_note_type("Unused")

        assert offer.note_type_state("Unused") == IDLE
        assert offer.note_types == set()

    def test_a_chosen_one_and_a_needed_one_are_told_apart(self):
        # Different colours because they mean different things: one arrived because a deck needs
        # it, the other was chosen. Un-choosing the deck takes only the first away.
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")
        offer.toggle_note_type("Unused")

        assert offer.note_type_state("Cloze") == NEEDED
        assert offer.note_type_state("Unused") == PICKED

    def test_dropping_one_that_was_also_picked_outright_really_drops_it(self):
        # A chip can be lit two ways. Picked first, then needed by a deck chosen afterwards —
        # and without un-choosing it, the union put it straight back: the chip said "dropped",
        # the tally said one was left behind, and the notes travelled anyway.
        offer = _offer()
        offer.toggle_note_type("Vocab")  # idle -> picked, no deck yet
        offer.pick_deck("Japanese::Kanji")  # now needed as well

        offer.toggle_note_type("Vocab")  # the user drops it

        assert offer.note_type_state("Vocab") == DROPPED
        assert "Vocab" not in offer.note_types, "a dropped note type still travels"
        assert offer.tally()["note_types"] == 1

    def test_a_drop_survives_picking_another_deck(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")
        offer.toggle_note_type("Vocab")

        offer.pick_deck("English")

        assert offer.note_type_state("Vocab") == DROPPED

    def test_the_tally_says_how_many_were_left_behind(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")
        offer.toggle_note_type("Vocab")

        tally = offer.tally()

        assert tally["note_types"] == 1
        assert tally["dropped"] == 1


class TestWhatTheSourceIsTold:
    def test_the_outright_choices_are_reported_separately(self):
        # The source cannot tell them apart from the filter list once decks are named — "in the
        # list because a deck needs it" and "because the user asked for it" look identical — and
        # guessing wrong means a chosen note type arriving as nothing at all.
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")  # needs Cloze and Vocab
        offer.toggle_note_type("Unused")  # chosen outright

        assert offer.chosen_note_types == {"Unused"}
        assert "Unused" in offer.note_types

    def test_a_note_type_only_needed_by_a_deck_is_not_reported_as_chosen(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")

        assert offer.chosen_note_types == set()


class TestTheSettings:
    def test_each_feature_is_picked_on_its_own(self):
        offer = _offer()

        offer.pick_feature("smart_notes")

        assert offer.features == {"smart_notes"}
        assert offer.state_of("features", "audio_speed") == UNPICKED

    def test_clicking_again_unpicks_it(self):
        offer = _offer()
        offer.pick_feature("smart_notes")

        answer = offer.pick_feature("smart_notes")

        assert answer["features"]["smart_notes"] == UNPICKED
        assert offer.features == set()

    def test_they_are_independent_of_the_decks(self):
        offer = _offer()

        offer.pick_feature("smart_notes")

        assert offer.decks == set()
        assert offer.tally()["features"] == 1


class TestTheTally:
    def test_it_counts_what_is_actually_coming(self):
        offer = _offer()
        offer.pick_deck("Japanese")
        offer.pick_feature("audio_speed")

        tally = offer.tally()

        assert tally["decks"] == 2  # Japanese and Japanese::Kanji
        assert tally["cards"] == 105
        assert tally["note_types"] == 3
        assert tally["features"] == 1

    def test_an_untouched_offer_counts_nothing(self):
        assert _offer().tally() == {
            "decks": 0,
            "cards": 0,
            "note_types": 0,
            "dropped": 0,
            "features": 0,
        }


class TestAskingAboutOneThing:
    def test_a_deck_reports_its_own_state(self):
        offer = _offer()
        offer.pick_deck("Japanese::Kanji")

        assert offer.state_of("decks", "Japanese") == "partial"
        assert offer.state_of("decks", "Japanese::Kanji") == PICKED

    def test_a_deck_that_is_not_there_reports_nothing(self):
        assert _offer().state_of("decks", "Gone") is None
