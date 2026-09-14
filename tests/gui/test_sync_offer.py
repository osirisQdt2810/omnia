"""Tests for the picker page: the tree it renders, and what it refuses to decide for the user.

The rules live in :mod:`omnia.core.sync.tree` and are tested next door. What is here is what the
PAGE has to get right for those rules to be usable: only the top level visible to begin with,
every row carrying the name the script navigates by, and no second model of the structure that
could drift from what is on screen.
"""

from __future__ import annotations

from omnia.core.sync import ConfigSummary, DeckEntry, Inventory, NoteTypeEntry
from omnia.gui.sync.offer_html import build_offer_html


def _inventory(**kwargs) -> Inventory:
    values = {
        "machine": "mac-mini",
        "decks": (
            DeckEntry(id=1, name="Japanese", cards=5),
            DeckEntry(id=2, name="Japanese::Kanji", cards=100),
            DeckEntry(id=3, name="Japanese::Kanji::N5", cards=30),
            DeckEntry(id=4, name="English", cards=7),
        ),
        "note_types": (NoteTypeEntry(name="Basic", fields=("Front", "Back")),),
        "config": ConfigSummary(features=("smart_notes", "audio_speed")),
    }
    values.update(kwargs)
    return Inventory(**values)


def _page(**kwargs) -> str:
    return build_offer_html(_inventory(**kwargs), dark=False)


class TestTheTreeItRenders:
    def test_it_names_the_machine_it_is_showing(self):
        assert "mac-mini" in _page()

    def test_every_deck_is_on_the_page(self):
        page = _page()

        for name in ("Japanese", "Kanji", "N5", "English"):
            assert f">{name}<" in page

    def test_only_the_top_level_starts_visible(self):
        # Hundreds of decks opening at once is a list nobody reads. One click opens one level.
        page = _page()

        assert 'data-deck="Japanese" data-depth="0"' in page
        # Counted on the rendered ROWS: the class name also appears in the CSS and the script,
        # which the page inlines, so a bare substring count can never be right.
        assert page.count('class="offer-row offer-hidden"') == 2  # Kanji and N5
        assert page.count('class="offer-row"') == 2  # Japanese and English

    def test_each_row_carries_its_full_name(self):
        # The script reads structure out of the NAMES — a sub-deck's name starts with its
        # parent's, plus "::" — rather than keeping a second model that can disagree with the
        # page. So the full name has to be on the element, not just the leaf.
        page = _page()

        assert 'data-deck="Japanese::Kanji::N5"' in page

    def test_only_a_deck_with_children_gets_an_arrow_to_press(self):
        page = _page()

        assert page.count('<button type="button" class="offer-arrow"') == 2
        assert "offer-arrow-none" in page  # the leaves get a spacer, so nothing jumps

    def test_a_parent_says_what_its_whole_subtree_holds(self):
        # A parent is picked for what is UNDER it, so that is the number it shows. A deck with
        # five cards of its own and 130 below must not read as "5".
        page = _page()

        assert '135 <span class="offer-total">in all</span>' in page
        assert "· 5 here" in page  # ...with its own five as a footnote

    def test_a_leaf_just_shows_its_cards(self):
        page = _page()

        assert '<span class="offer-cards">7</span>' in page  # English
        assert "7 <span" not in page

    def test_a_parent_holding_nothing_itself_does_not_say_so(self):
        page = _page(
            decks=(
                DeckEntry(id=1, name="Japanese", cards=0),
                DeckEntry(id=2, name="Japanese::Kanji", cards=100),
            )
        )

        assert "here</span>" not in page

    def test_the_count_the_script_adds_up_is_on_the_element(self):
        # Not scraped from the rendered text: that is a sentence for a person, and pulling
        # digits out of "135 in all · 5 here" would add a subtree to its own total.
        assert 'data-cards="5"' in _page()

    def test_a_machine_with_no_decks_says_so(self):
        assert "no decks on that computer" in _page(decks=())

    def test_a_deck_name_cannot_inject_markup(self):
        page = _page(
            decks=(DeckEntry(id=1, name="<img src=x onerror=alert(1)>", cards=1),)
        )

        assert "<img src=x" not in page
        assert "&lt;img" in page


class TestTheNoteTypesAndTheSettings:
    def test_every_note_type_is_a_chip_you_can_click(self):
        page = _page()

        assert 'data-kind="note-type" data-name="Basic"' in page

    def test_they_start_idle_because_no_deck_has_been_picked_yet(self):
        # Dimmed rather than hidden: what is over there is worth seeing even when none of it is
        # coming, and a list that appears from nowhere on the first click is disorienting.
        page = _page()

        assert page.count('class="offer-chip offer-idle"') >= 1

    def test_a_note_type_says_how_many_notes_use_it(self):
        # The number that says whether it matters.
        page = _page(
            note_types=(NoteTypeEntry(name="Basic", fields=("Front",), notes=1200),)
        )

        assert "1,200" in page

    def test_it_explains_both_things_a_click_can_mean(self):
        # A chip does one of two opposite things depending on where it stands, and neither is
        # obvious from the chip alone.
        page = _page().lower()

        assert "leave it behind and the deck still comes without those notes" in page
        assert "bring just its definition, with no notes at all" in page

    def test_every_configured_feature_is_its_own_chip(self):
        page = _page()

        assert 'data-kind="feature" data-name="smart_notes"' in page
        assert 'data-kind="feature" data-name="audio_speed"' in page

    def test_it_says_that_keys_do_not_travel(self):
        # The one thing a reader will want to know before ticking any of them.
        assert "API keys never travel" in _page()

    def test_a_machine_with_nothing_configured_says_so(self):
        page = _page(config=ConfigSummary(features=()))

        assert "Nothing is configured over there." in page

    def test_a_long_list_is_shown_in_full_rather_than_truncated(self):
        # Every one of them is a thing you might click, so summarising would hide choices.
        page = _page(
            note_types=tuple(NoteTypeEntry(name=f"Type {n}") for n in range(20))
        )

        assert page.count('data-kind="note-type"') == 20

    def test_a_note_type_name_cannot_inject_markup(self):
        page = _page(note_types=(NoteTypeEntry(name='"><script>x</script>'),))

        assert "<script>x</script>" not in page


class TestTheButton:
    def test_it_starts_disabled_because_nothing_is_picked_yet(self):
        assert 'id="offer-go" disabled' in _page()

    def test_the_page_never_mentions_the_transport(self):
        lowered = _page().lower()

        for word in ("tailscale", "vpn", "ip address", "http", "port "):
            assert word not in lowered


class TestWhatAClickAnswersWith:
    """The page paints; it does not decide. These are the answers it paints from.

    The rule has a corner in it — unpicking a parent whose child was picked separately — and the
    version where the script worked this out itself got it wrong in a way no test could see: it
    queried its own set while still mutating it, so every expandable SIBLING of a picked deck
    came out looking half-selected. It was found by driving a real window.
    """

    def _selection(self):
        from omnia.core.sync.tree import DeckSelection, DeckTree

        tree = DeckTree.from_entries(_inventory().decks)
        return tree, DeckSelection(tree)

    def test_picking_a_parent_answers_for_its_whole_subtree(self):
        _tree, selection = self._selection()

        states = selection.apply("Japanese")

        assert states == {
            "Japanese": "picked",
            "Japanese::Kanji": "picked",
            "Japanese::Kanji::N5": "picked",
        }

    def test_picking_a_child_answers_for_the_parents_above_it(self):
        _tree, selection = self._selection()

        states = selection.apply("Japanese::Kanji")

        assert states["Japanese::Kanji"] == "picked"
        assert states["Japanese::Kanji::N5"] == "picked"
        assert states["Japanese"] == "partial"

    def test_it_never_answers_for_a_deck_the_click_did_not_touch(self):
        # The bug this closes: siblings repainted as partial when nothing under them was picked.
        _tree, selection = self._selection()

        states = selection.apply("Japanese::Kanji")

        assert "English" not in states

    def test_unpicking_a_parent_leaves_nothing_half_lit(self):
        _tree, selection = self._selection()
        selection.apply("Japanese::Kanji")
        selection.apply("Japanese")

        states = selection.apply("Japanese")

        assert set(states.values()) == {"unpicked"}

    def test_a_deck_the_tree_does_not_know_still_gets_an_answer(self):
        _tree, selection = self._selection()

        assert selection.apply("Gone") == {"Gone": "picked"}
