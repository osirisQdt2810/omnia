"""What one machine offers another: the deck tree, the note types, and the configuration.

This is the first thing the target machine sees after it connects, and it is what the user
chooses from — so it answers exactly the questions a person asks before deciding to pull, and
nothing else:

* **which decks are here, and how big.** With the sub-deck structure intact, because "sync one
  deck" almost always means a parent and everything under it, and a flat list of forty names
  makes the user rebuild that tree in their head.
* **which note types those decks need.** Each deck carries the note types ITS cards use, so the
  target can light them up the moment a deck is chosen rather than asking a second question per
  deck over a link that may be a laptop across a VPN.
* **what configuration exists**, summarised: which features are configured, and which note types
  the per-note-type rules cover. The rules themselves are not in the inventory — they are large,
  and the target has not decided to take anything yet.

It carries **no card contents and no media**: an inventory is a menu, and a menu that costs forty
megabytes to render is the thing this feature exists to avoid.

Pure data — no ``aqt``, no ``anki`` — so the shape round-trips in tests without a collection.
Reading a real collection into one lives in :mod:`omnia.core.sync.service`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Bumped when a field changes meaning or disappears. The target refuses an inventory it cannot
#: read rather than guessing, and says WHICH SIDE is behind — a sync that half-understands the
#: other machine is worse than one that stops.
PROTOCOL = 2


class InventoryError(ValueError):
    """An inventory that cannot be read, with a reason a person can act on."""


@dataclass(frozen=True)
class DeckEntry:
    """One deck on the source machine."""

    #: The deck's id on the SOURCE. Meaningless on the target — deck ids are per-collection — and
    #: carried only so a later request can name this deck unambiguously while the session lasts.
    #: What gets matched on arrival is the NAME.
    id: int
    #: The full name, ``Parent::Child`` as Anki stores it, so the tree is reconstructible without
    #: a second field.
    name: str
    #: Cards in this deck ALONE, not counting sub-decks. A parent that showed its children's total
    #: would read as "5000 cards" for a deck that holds none, and the user picks by size.
    cards: int = 0
    #: The note types this deck's own cards use, by name. Carried so the target can light up the
    #: note types a chosen deck needs the instant it is chosen — the alternative is asking the
    #: source a second question per deck, over a link that may be a laptop on the other side of
    #: a VPN. Names rather than ids, because ids are per-collection and mean nothing here.
    note_types: tuple[str, ...] = ()

    @property
    def parent(self) -> str:
        """The parent deck's full name, or "" for a top-level deck."""
        return self.name.rsplit("::", 1)[0] if "::" in self.name else ""


@dataclass(frozen=True)
class NoteTypeEntry:
    """One note type, and what on the source is using it."""

    name: str
    fields: tuple[str, ...] = ()
    #: How many notes use it. A note type with no notes still ships when a chosen deck references
    #: it — an empty deck is a legitimate thing to want — but the number tells the user what they
    #: are about to bring.
    notes: int = 0


@dataclass(frozen=True)
class ConfigSummary:
    """What configuration the source holds, without the contents."""

    #: Feature sections that are configured (``smart_notes``, ``audio_speed``, …).
    features: tuple[str, ...] = ()
    #: Note types the per-note-type rules cover. The target skips the ones it has no note type
    #: for, and this is what lets it say so BEFORE anything is pulled.
    rule_note_types: tuple[str, ...] = ()
    #: User-authored tools by name. They travel, but never load themselves on arrival — ADR-013:
    #: code approved on one machine must not execute on another because it was copied there.
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Inventory:
    """Everything the target needs in order to choose, and nothing it does not."""

    machine: str = ""
    omnia_version: str = ""
    protocol: int = PROTOCOL
    decks: tuple[DeckEntry, ...] = ()
    note_types: tuple[NoteTypeEntry, ...] = ()
    config: ConfigSummary = field(default_factory=ConfigSummary)

    def to_json(self) -> str:
        """Render as the JSON the session serves."""
        return json.dumps(
            {
                "machine": self.machine,
                "omnia_version": self.omnia_version,
                "protocol": self.protocol,
                "decks": [
                    {
                        "id": deck.id,
                        "name": deck.name,
                        "cards": deck.cards,
                        "note_types": list(deck.note_types),
                    }
                    for deck in self.decks
                ],
                "note_types": [
                    {
                        "name": note_type.name,
                        "fields": list(note_type.fields),
                        "notes": note_type.notes,
                    }
                    for note_type in self.note_types
                ],
                "config": {
                    "features": list(self.config.features),
                    "rule_note_types": list(self.config.rule_note_types),
                    "tools": list(self.config.tools),
                },
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Inventory:
        """Read an inventory the other machine served.

        Args:
            text: The response body.

        Returns:
            The inventory.

        Raises:
            InventoryError: When the body is not an inventory, or is one this build cannot read.
                A mismatched protocol is refused by NUMBER rather than by whatever field happens
                to be missing, so the message can say which machine to update instead of
                surfacing a KeyError from three layers down.
        """
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise InventoryError(
                "the other machine did not answer with an inventory"
            ) from exc
        if not isinstance(raw, dict):
            raise InventoryError("the other machine did not answer with an inventory")
        protocol = _int(raw.get("protocol"), 0)
        if protocol > PROTOCOL:
            raise InventoryError(
                "the other machine runs a newer Omnia than this one — update this machine "
                "before syncing from it"
            )
        if protocol < PROTOCOL:
            raise InventoryError(
                "the other machine runs an older Omnia than this one — update that machine "
                "before syncing from it"
            )
        config = raw.get("config") or {}
        return cls(
            machine=str(raw.get("machine", "")),
            omnia_version=str(raw.get("omnia_version", "")),
            protocol=protocol,
            decks=tuple(
                DeckEntry(
                    id=_int(entry.get("id"), 0),
                    name=str(entry.get("name", "")),
                    cards=_int(entry.get("cards"), 0),
                    note_types=tuple(
                        str(name) for name in entry.get("note_types") or ()
                    ),
                )
                for entry in _entries(raw.get("decks"))
                if str(entry.get("name", ""))
            ),
            note_types=tuple(
                NoteTypeEntry(
                    name=str(entry.get("name", "")),
                    fields=tuple(str(name) for name in entry.get("fields") or ()),
                    notes=_int(entry.get("notes"), 0),
                )
                for entry in _entries(raw.get("note_types"))
                if str(entry.get("name", ""))
            ),
            config=ConfigSummary(
                features=tuple(str(name) for name in config.get("features") or ()),
                rule_note_types=tuple(
                    str(name) for name in config.get("rule_note_types") or ()
                ),
                tools=tuple(str(name) for name in config.get("tools") or ()),
            ),
        )

    def deck_tree(self) -> list[tuple[DeckEntry, int]]:
        """The decks as ``(deck, depth)`` in display order, parents before children.

        Depth comes from the NAME rather than from the order, so a source that serves its decks
        unsorted still renders as a tree — and a child whose parent is missing from the list (a
        filtered deck's home, say) still appears rather than vanishing into a gap.
        """
        return [
            (deck, deck.name.count("::"))
            for deck in sorted(self.decks, key=lambda deck: deck.name.lower())
        ]


def _entries(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a list field, ignoring anything else in it."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _int(value: Any, fallback: int) -> int:
    """An int, or ``fallback`` — a count that arrives as a string must not break the menu."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
