"""Tests for what a pull would land on top of, and what the user is told about it.

A deck or note type with the same NAME already here is not an error — two machines syncing one
collection through AnkiWeb share most of both — but it is the only way an arriving package can
change something that was already here, so it is the one thing to say before the copy runs.

The two clash for different reasons and the sentences have to say which, or the warning is noise
that gets clicked through.
"""

from __future__ import annotations

import pytest

from omnia.core.sync import DeckEntry, Inventory, NoteTypeEntry
from omnia.core.sync.clash import (
    KEEP,
    OVERRIDE,
    Clashes,
    NoteTypeClash,
    describe,
    find_clashes,
    normalise,
)


def _inventory(decks=(), note_types=()) -> Inventory:
    return Inventory(
        decks=tuple(DeckEntry(id=i + 1, name=n) for i, n in enumerate(decks)),
        note_types=tuple(NoteTypeEntry(name=n, fields=tuple(f)) for n, f in note_types),
    )


class TestWhatItFinds:
    def test_nothing_in_common_is_no_clash(self):
        clashes = find_clashes(
            decks=["Japanese"],
            note_types=["Basic"],
            here=_inventory(decks=["English"], note_types=[("Cloze", ["Text"])]),
            there=_inventory(),
        )

        assert not clashes

    def test_a_deck_already_here_is_named(self):
        clashes = find_clashes(
            decks=["Japanese", "English"],
            note_types=[],
            here=_inventory(decks=["English"]),
            there=_inventory(),
        )

        assert clashes.decks == ("English",)
        assert bool(clashes)

    def test_a_note_type_with_the_same_fields_is_not_serious(self):
        # Two machines syncing one collection share these; saying so is worth a line, not an
        # alarm.
        fields = ("Front", "Back")
        clashes = find_clashes(
            decks=[],
            note_types=["Basic"],
            here=_inventory(note_types=[("Basic", fields)]),
            there=_inventory(note_types=[("Basic", fields)]),
        )

        assert clashes.note_types[0].fields_differ is False
        assert clashes.serious == ()

    def test_a_note_type_with_different_fields_is_the_serious_one(self):
        # A field added on this machine. Merging is what empties it on every note that used it.
        clashes = find_clashes(
            decks=[],
            note_types=["Basic"],
            here=_inventory(note_types=[("Basic", ("Front", "Back", "Notes"))]),
            there=_inventory(note_types=[("Basic", ("Front", "Back"))]),
        )

        assert clashes.serious[0].name == "Basic"
        assert clashes.serious[0].here == ("Front", "Back", "Notes")
        assert clashes.serious[0].there == ("Front", "Back")

    def test_a_note_type_only_on_the_other_machine_is_not_a_clash(self):
        clashes = find_clashes(
            decks=[],
            note_types=["Brand New"],
            here=_inventory(note_types=[("Basic", ("Front",))]),
            there=_inventory(note_types=[("Brand New", ("A",))]),
        )

        assert clashes.note_types == ()

    def test_names_come_back_sorted_so_the_warning_reads_the_same_every_time(self):
        clashes = find_clashes(
            decks=["Z", "A", "M"],
            note_types=[],
            here=_inventory(decks=["A", "M", "Z"]),
            there=_inventory(),
        )

        assert clashes.decks == ("A", "M", "Z")


class TestWhatItSays:
    def _serious(self) -> Clashes:
        return Clashes(
            note_types=(
                NoteTypeClash(
                    "Basic", True, ("Front", "Back", "Notes"), ("Front", "Back")
                ),
            )
        )

    def test_a_mismatched_note_type_says_what_can_be_lost(self):
        lines = describe(self._serious(), KEEP)

        assert "DIFFERENT fields" in lines[0]
        assert "leave a field empty" in lines[0]

    def test_the_serious_one_is_said_first(self):
        clashes = Clashes(
            decks=("Japanese",),
            note_types=(NoteTypeClash("Basic", True), NoteTypeClash("Cloze", False)),
        )

        lines = describe(clashes, KEEP)

        assert "DIFFERENT fields" in lines[0]

    def test_a_deck_clash_says_nothing_is_removed(self):
        # The reassurance that stops somebody cancelling a copy that was going to be fine.
        lines = describe(Clashes(decks=("Japanese",)), KEEP)

        assert any("nothing in it is removed" in line.lower() for line in lines)

    def test_the_duplicate_policy_is_always_stated(self):
        # Whether or not anything clashed: it decides what happens to notes that exist on both,
        # and the user is about to press a button that acts on it.
        assert any("left as it is here" in line for line in describe(Clashes(), KEEP))
        assert any("REPLACED" in line for line in describe(Clashes(), OVERRIDE))

    def test_a_long_list_is_summarised_rather_than_dumped(self):
        clashes = Clashes(decks=tuple(f"Deck {n}" for n in range(9)))

        lines = describe(clashes, KEEP)

        assert "and 5 more" in lines[0]


class TestThePolicy:
    def test_keeping_is_the_default(self):
        # A copy that silently replaced an edit made on this machine is the one outcome nobody
        # asks for and nobody notices until much later.
        assert normalise(None) == KEEP
        assert normalise("") == KEEP

    @pytest.mark.parametrize("stored", ["override", "OVERRIDE", " Override "])
    def test_it_is_read_case_and_space_insensitively(self, stored):
        assert normalise(stored) == OVERRIDE

    def test_a_value_nobody_recognises_falls_back_to_keeping(self):
        # A hand-edited config with a typo must not be read as permission to overwrite.
        assert normalise("overide") == KEEP
        assert normalise(True) == KEEP
