"""Tests for the deck tree and what picking a deck means.

Anki stores decks flat, as ``Parent::Child`` names, and everything the picker needs is derivable
from those names. Two rules carry the feature and both are here rather than in the page:

* picking a deck picks its sub-decks, and so does unpicking it;
* a parent whose children were picked one at a time is PARTIAL, never picked — drawing it as
  picked would promise to bring a parent nobody chose.
"""

from __future__ import annotations

from omnia.core.sync.inventory import DeckEntry
from omnia.core.sync.tree import (
    PARTIAL,
    PICKED,
    UNPICKED,
    DeckSelection,
    DeckTree,
)


def _tree(*names_and_counts: tuple[str, int]) -> DeckTree:
    return DeckTree.from_entries(
        DeckEntry(id=index + 1, name=name, cards=cards)
        for index, (name, cards) in enumerate(names_and_counts)
    )


def _japanese() -> DeckTree:
    return _tree(
        ("Japanese", 5),
        ("Japanese::Kanji", 100),
        ("Japanese::Kanji::N5", 30),
        ("Japanese::Grammar", 12),
        ("English", 7),
    )


class TestTheShapeOfTheTree:
    def test_children_hang_off_their_parent(self):
        tree = _japanese()

        japanese = tree.node("Japanese")
        assert [child.name for child in japanese.children] == [
            "Japanese::Grammar",
            "Japanese::Kanji",
        ]

    def test_structure_comes_from_the_names_not_the_order_they_arrived_in(self):
        # A source is free to serve its decks unsorted; the tree must not depend on it.
        scrambled = _tree(
            ("Japanese::Kanji::N5", 30),
            ("English", 7),
            ("Japanese", 5),
            ("Japanese::Kanji", 100),
        )

        assert [row.node.name for row in scrambled.rows()] == [
            "English",
            "Japanese",
            "Japanese::Kanji",
            "Japanese::Kanji::N5",
        ]

    def test_a_deck_whose_parent_is_missing_becomes_a_root(self):
        # A filtered deck's home, or a collection mid-edit. It has to appear somewhere rather
        # than vanish into a gap.
        tree = _tree(("Zebra::Orphan", 1))

        assert [row.node.name for row in tree.rows()] == ["Zebra::Orphan"]
        assert tree.rows()[0].depth == 0

    def test_a_root_keeps_its_whole_name_and_a_child_shows_only_its_leaf(self):
        # The indentation says where a child sits; repeating the path on every descendant is
        # noise. A root has nothing above it to give that context, so it keeps all of it.
        rows = {
            row.node.name: row.label
            for row in _tree(("Zebra::Orphan", 1), ("A", 1), ("A::B", 1)).rows()
        }

        assert rows["Zebra::Orphan"] == "Zebra::Orphan"
        assert rows["A::B"] == "B"

    def test_only_decks_with_children_get_an_arrow(self):
        expandable = {row.node.name: row.expandable for row in _japanese().rows()}

        assert expandable["Japanese"] is True
        assert expandable["Japanese::Kanji"] is True
        assert expandable["English"] is False

    def test_a_deck_counts_its_own_cards_and_reports_its_subtree_separately(self):
        # A parent that showed its children's total would read as "135 cards" for a deck that
        # holds five, and the user picks by size.
        japanese = _japanese().node("Japanese")

        assert japanese.cards == 5
        assert japanese.total_cards == 147


class TestPickingADeck:
    def test_it_picks_the_sub_decks_too(self):
        tree = _japanese()
        selection = DeckSelection(tree)

        assert selection.toggle("Japanese") is True
        assert selection.names == {
            "Japanese",
            "Japanese::Kanji",
            "Japanese::Kanji::N5",
            "Japanese::Grammar",
        }

    def test_clicking_again_unpicks_the_same_subtree(self):
        selection = DeckSelection(_japanese())
        selection.toggle("Japanese")

        assert selection.toggle("Japanese") is False
        assert selection.names == set()

    def test_unpicking_a_parent_clears_a_child_that_was_picked_on_its_own(self):
        # The user clicked a deck that was lit and expects it to go dark. A child left behind
        # would light the parent again as partial, which reads as the click not having worked.
        tree = _japanese()
        selection = DeckSelection(tree)
        selection.toggle("Japanese::Kanji")
        selection.toggle("Japanese")

        selection.toggle("Japanese")

        assert selection.names == set()

    def test_it_does_not_touch_a_sibling(self):
        tree = _japanese()
        selection = DeckSelection(tree)

        selection.toggle("Japanese::Kanji")

        assert "English" not in selection.names
        assert "Japanese::Grammar" not in selection.names

    def test_a_deck_the_tree_does_not_know_is_still_picked(self):
        # The page and the tree can disagree for a moment; ignoring the click is the one answer
        # that leaves no trace of what happened.
        selection = DeckSelection(_japanese())

        assert selection.toggle("Gone") is True
        assert selection.names == {"Gone"}

    def test_the_card_tally_counts_each_deck_once(self):
        selection = DeckSelection(_japanese())
        selection.toggle("Japanese")

        assert selection.cards == 5 + 100 + 30 + 12


class TestWhatARowShows:
    def test_a_picked_deck_and_its_children_are_picked(self):
        tree = _japanese()
        selection = DeckSelection(tree)
        selection.toggle("Japanese")

        assert selection.state(tree.node("Japanese")) == PICKED
        assert selection.state(tree.node("Japanese::Kanji::N5")) == PICKED

    def test_a_parent_of_a_picked_child_is_partial_not_picked(self):
        tree = _japanese()
        selection = DeckSelection(tree)
        selection.toggle("Japanese::Kanji")

        assert selection.state(tree.node("Japanese")) == PARTIAL

    def test_an_untouched_deck_is_unpicked(self):
        tree = _japanese()

        assert DeckSelection(tree).state(tree.node("English")) == UNPICKED

    def test_a_sibling_of_a_picked_deck_stays_unpicked(self):
        tree = _japanese()
        selection = DeckSelection(tree)
        selection.toggle("Japanese::Kanji")

        assert selection.state(tree.node("Japanese::Grammar")) == UNPICKED
