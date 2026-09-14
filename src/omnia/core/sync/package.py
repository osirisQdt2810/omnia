"""What the target asks the source to pack, and what that request means.

A pull moves an Anki package: the chosen decks, the notes in them, the note types those notes
need, and **the media** — a card whose audio and images stayed behind is not the card the user
picked. Media is also the bulk of it, which is why :mod:`omnia.core.sync.progress` exists.

Everything undecidable is decided here rather than at either end of the wire:

* **Note types do two jobs, and which one depends on whether decks were named.** With decks, they
  are a restriction on the CARDS — a deck holding two note types where one was dropped must arrive
  with only the other's notes. Without decks, they are the whole request: bring these note types'
  DEFINITIONS and no notes at all, which is how a second machine is set up to author a kind of
  card before there is anything to put in it.
* **An empty request is refused, never widened.** Nothing named at all means nothing to send, and
  decks with every note type dropped means every note was excluded; either could be read as "then
  send everything", and that reading is how somebody ends up with a stranger's whole collection
  because a checkbox did not register. The refusal lives here so it holds at BOTH ends.

Pure: it builds a description, not a search string, because the escaping belongs to Anki's own
search builder and this module may not import it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class PackageError(ValueError):
    """A request that cannot be packed, with a reason a person can act on."""


@dataclass(frozen=True)
class PackageRequest:
    """What one machine is asking another to pack up."""

    #: Full deck names, sub-decks listed explicitly — the selection has already expanded them.
    decks: tuple[str, ...] = ()
    #: Note type names whose notes may travel. Always the complete list of what should come; an
    #: empty one means everything was dropped, not that there is no restriction.
    note_types: tuple[str, ...] = ()
    #: Note types the user asked for OUTRIGHT — their definitions travel whether or not any cards
    #: use them. Carried separately rather than inferred at the source, which with decks named
    #: cannot tell "in the filter because a deck needs it" from "because the user asked for it";
    #: guessing wrong means a chosen note type arriving as nothing at all.
    definitions: tuple[str, ...] = ()
    #: Config sections to copy. Not part of the package — they ride in the same answer because
    #: they are part of the same decision.
    config: tuple[str, ...] = ()
    #: Whether to bring review history. Off would make every card new on arrival, which is the
    #: right default for a second machine that is catching up rather than taking over.
    with_scheduling: bool = True
    #: Media always travels. Declared rather than assumed so the wire says so, and so a future
    #: "just the text" option has somewhere to live.
    with_media: bool = True

    def __post_init__(self) -> None:
        """Refuse a request that cannot mean anything.

        Raises:
            PackageError: When it names no decks, or no note types while naming decks. Refused
                rather than widened: either could be read as "send everything", and that reading
                is how a mis-registered checkbox turns into somebody's whole collection.
        """
        if (
            not self.decks
            and not self.note_types
            and not self.definitions
            and not self.config
        ):
            raise PackageError("nothing was chosen to copy")
        if self.decks and not self.note_types:
            raise PackageError(
                "every note type was left behind, so those decks would arrive empty"
            )

    @property
    def wants_cards(self) -> bool:
        """Whether any cards are being asked for.

        False for a request that names only note types or only settings — and the source must
        check this before it builds a search, because a card search with no deck in it is not a
        narrow search, it is the whole collection.
        """
        return bool(self.decks)

    def to_json(self) -> str:
        """Render for the wire."""
        return json.dumps(
            {
                "decks": list(self.decks),
                "note_types": list(self.note_types),
                "definitions": list(self.definitions),
                "config": list(self.config),
                "with_scheduling": self.with_scheduling,
                "with_media": self.with_media,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> PackageRequest:
        """Read a request the other machine sent.

        Raises:
            PackageError: When it is not a request, or is one that cannot mean anything.
        """
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise PackageError("that was not a request for a package") from exc
        if not isinstance(raw, dict):
            raise PackageError("that was not a request for a package")
        return cls(
            decks=_names(raw.get("decks")),
            note_types=_names(raw.get("note_types")),
            definitions=_names(raw.get("definitions")),
            config=_names(raw.get("config")),
            with_scheduling=bool(raw.get("with_scheduling", True)),
            with_media=bool(raw.get("with_media", True)),
        )


@dataclass(frozen=True)
class PackageOffer:
    """What the source packed, answered before a byte of the package moves.

    The size is the whole point: it is what turns the copy into a percentage and a time estimate
    rather than a spinner. A source that cannot say how big it is sends 0, and the target shows a
    moving bar with no number instead of a made-up one.

    Two things ride in this answer rather than inside the package, because an ``.apkg`` cannot
    carry either: the **settings**, which are not Anki's to store, and the **definitions** of note
    types chosen with no notes behind them — Anki gathers note types from the notes it is
    exporting, so one with no cards contributes nothing to a package at all.
    """

    id: str
    bytes: int = 0
    cards: int = 0
    notes: int = 0
    #: ``{section: values}`` for the settings that were asked for.
    config: dict[str, Any] = field(default_factory=dict)
    #: Note type definitions, as Anki stores them, for the ones chosen on their own.
    note_types: tuple[dict[str, Any], ...] = ()

    @property
    def has_package(self) -> bool:
        """Whether there is a file to fetch. False when only settings or definitions travel."""
        return bool(self.id) and self.bytes > 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "bytes": self.bytes,
                "cards": self.cards,
                "notes": self.notes,
                "config": self.config,
                "note_types": list(self.note_types),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> PackageOffer:
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise PackageError(
                "the other machine did not answer with a package"
            ) from exc
        if not isinstance(raw, dict) or "id" not in raw:
            raise PackageError("the other machine did not answer with a package")
        config = raw.get("config")
        note_types = raw.get("note_types")
        return cls(
            id=str(raw["id"]),
            bytes=_count(raw.get("bytes")),
            cards=_count(raw.get("cards")),
            notes=_count(raw.get("notes")),
            config=config if isinstance(config, dict) else {},
            note_types=tuple(
                entry for entry in (note_types or ()) if isinstance(entry, dict)
            ),
        )


def _names(value: Any) -> tuple[str, ...]:
    """The non-empty strings of a list field, in order, without duplicates."""
    if not isinstance(value, list):
        return ()
    seen: dict[str, None] = {}
    for item in value:
        name = str(item)
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _count(value: Any) -> int:
    """A count, or 0 — a size that arrives as a string must not break the transfer."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
