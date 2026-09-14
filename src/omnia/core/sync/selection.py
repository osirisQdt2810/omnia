"""What the user has chosen to take, across all three parts of an offer.

One object rather than three, because the parts are not independent: choosing a deck lights up
the note types that deck's cards need, and un-lighting one of those changes what choosing the
deck means. Splitting them would put that relationship in whichever screen happened to render
them, which is where it would rot.

The three parts, and the rule each follows:

* **Decks** — clicking one takes it and everything under it; clicking again puts all of it back.
* **Note types** — in one of three states, because there are two different things a user means.
  A note type a chosen deck NEEDS lights up on its own, and clicking it DROPS it: the deck still
  comes, the notes of that type stay behind. A note type nothing needs can be picked outright,
  and then its definition travels with no notes at all — which is how you set up a second machine
  to author the same kind of card before there is anything to put in it. The two wear different
  colours because they mean different things: one arrived, the other was chosen.
* **Settings** — one chip per configured feature, picked outright. Credentials are not among
  them; the source never offers those.

Pure — no ``aqt``, no HTML — so every rule here is tested without a window.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core.sync.tree import DeckSelection, DeckTree

#: What a note-type chip shows. ``NEEDED`` is its own state and deliberately not "picked": it was
#: not chosen, it arrived because a deck needs it, and drawing the two the same would hide the
#: fact that un-choosing the deck takes it away again.
#: Lit because a chosen deck needs it. Deliberately not "picked": it was not chosen, and drawing
#: the two the same would hide that un-choosing the deck takes it away again.
NEEDED = "needed"
#: Needed by a chosen deck, and deliberately left behind.
DROPPED = "dropped"
#: Nothing needs it and nobody chose it.
IDLE = "idle"

PICKED = "picked"
UNPICKED = "unpicked"


class OfferSelection:
    """The whole of what one machine is being asked for.

    Args:
        inventory: What the other machine offers.
    """

    def __init__(self, inventory: Any) -> None:
        self._inventory = inventory
        self._tree = DeckTree.from_entries(inventory.decks)
        self._decks = DeckSelection(self._tree)
        # Deck name -> the note types ITS OWN cards use. Built once: it is asked for on every
        # click, and a click must not walk the whole inventory.
        self._needs: dict[str, frozenset[str]] = {
            entry.name: frozenset(entry.note_types) for entry in inventory.decks
        }
        self._dropped: set[str] = set()
        #: Note types chosen for their own sake, with no deck behind them.
        self._chosen: set[str] = set()
        self._features: set[str] = set()

    # --- decks --------------------------------------------------------------------------
    @property
    def tree(self) -> DeckTree:
        """The decks, as a tree."""
        return self._tree

    @property
    def decks(self) -> set[str]:
        """The decks currently taken."""
        return self._decks.names

    def pick_deck(self, name: str) -> dict[str, dict[str, str]]:
        """Toggle a deck, and report every row whose appearance changed — of any kind.

        Note types come back too: a deck's note types light up the moment it is taken, and go
        dark again when the last deck needing them is put back. The page repaints from this and
        holds no rule of its own.
        """
        deck_states = self._decks.apply(name)
        return {"decks": deck_states, "note_types": self._note_type_states()}

    # --- note types ---------------------------------------------------------------------
    @property
    def needed(self) -> set[str]:
        """The note types the taken decks need."""
        out: set[str] = set()
        for deck in self._decks.names:
            out |= self._needs.get(deck, frozenset())
        return out

    @property
    def chosen_note_types(self) -> set[str]:
        """The note types picked for their own sake, whether or not a deck also needs them.

        Sent to the source as its own list rather than inferred there: with decks named it cannot
        tell "this note type is in the filter because a deck needs it" from "…because the user
        asked for it", and guessing wrong means a chosen note type arriving as nothing at all.
        """
        return set(self._chosen)

    @property
    def note_types(self) -> set[str]:
        """The note types that will actually travel.

        The ones a chosen deck needs, minus the ones dropped, plus the ones chosen for their own
        sake. A note type can be in the last group and not the first: its definition travels with
        no notes, which is how a second machine is set up to author the same kind of card before
        there is anything to put in it.
        """
        return (self.needed - self._dropped) | self._chosen

    def toggle_note_type(self, name: str) -> dict[str, dict[str, str]]:
        """Click one note type, and answer with what it now shows.

        What a click MEANS depends on where the chip stands, and that is the point:

        * needed by a chosen deck → drop it, and the deck still comes without those notes;
        * already dropped → take it back;
        * nothing needs it → choose it outright, so its definition travels with no notes.

        One handler rather than two ops, because the user does one thing — they click a chip —
        and a page that had to know which of two messages to send would be holding the rule.
        """
        if name in self.needed:
            if name in self._dropped:
                self._dropped.discard(name)
            else:
                self._dropped.add(name)
                # Also un-choose it. A chip can be lit two ways — chosen outright, then needed by
                # a deck picked afterwards — and without this the union puts it straight back:
                # the chip read "dropped", the tally said one was left behind, and the notes
                # travelled anyway.
                self._chosen.discard(name)
        elif name in self._chosen:
            self._chosen.discard(name)
        else:
            self._chosen.add(name)
        return {"note_types": {name: self.note_type_state(name)}}

    def note_type_state(self, name: str) -> str:
        """What one note-type chip shows."""
        if name in self.needed:
            return DROPPED if name in self._dropped else NEEDED
        return PICKED if name in self._chosen else IDLE

    def _note_type_states(self) -> dict[str, str]:
        """Every note type's state — cheap, and it keeps the page from deriving any of them."""
        return {
            entry.name: self.note_type_state(entry.name)
            for entry in getattr(self._inventory, "note_types", ())
        }

    # --- settings -----------------------------------------------------------------------
    @property
    def features(self) -> set[str]:
        """The configured features being taken."""
        return set(self._features)

    def pick_feature(self, name: str) -> dict[str, dict[str, str]]:
        """Toggle one feature's settings."""
        if name in self._features:
            self._features.discard(name)
        else:
            self._features.add(name)
        return {"features": {name: PICKED if name in self._features else UNPICKED}}

    # --- the line above the button --------------------------------------------------------
    def tally(self) -> dict[str, Any]:
        """What is about to be copied, as numbers the page turns into a sentence."""
        return {
            "decks": len(self._decks.names),
            "cards": self._decks.cards,
            "note_types": len(self.note_types),
            "dropped": len(self.needed & self._dropped),
            "features": len(self._features),
        }

    def state_of(self, kind: str, name: str) -> Optional[str]:
        """One row's state, for a caller that wants to ask about a single thing."""
        if kind == "note_types":
            return self.note_type_state(name)
        if kind == "features":
            return PICKED if name in self._features else UNPICKED
        node = self._tree.node(name)
        return self._decks.state(node) if node is not None else None
