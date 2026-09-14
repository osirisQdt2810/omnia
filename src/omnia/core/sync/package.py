"""What the target asks the source to pack, and what that request means.

A pull moves an Anki package: the chosen decks, the notes in them, the note types those notes
need, and **the media** — a card whose audio and images stayed behind is not the card the user
picked. Media is also the bulk of it, which is why :mod:`omnia.core.sync.progress` exists.

Everything undecidable is decided here rather than at either end of the wire:

* **A request names decks AND note types, and both are restrictions.** The note types are not
  "which ones to bring along" — they are dragged along by the notes regardless — they are which
  notes may travel at all. A deck holding two note types where the user dropped one must arrive
  with only the other's notes in it, and that is a filter on the CARDS, not on the package.
* **An empty request is refused, never widened.** No decks means nothing to send, and no note
  types means every note was excluded; either could be read as "then send everything", and that
  reading is how somebody ends up with a stranger's whole collection because a checkbox did not
  register.

Pure: it builds a description, not a search string, because the escaping belongs to Anki's own
search builder and this module may not import it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
        if not self.decks and not self.config:
            raise PackageError("nothing was chosen to copy")
        if self.decks and not self.note_types:
            raise PackageError(
                "every note type was left behind, so those decks would arrive empty"
            )

    def to_json(self) -> str:
        """Render for the wire."""
        return json.dumps(
            {
                "decks": list(self.decks),
                "note_types": list(self.note_types),
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
            config=_names(raw.get("config")),
            with_scheduling=bool(raw.get("with_scheduling", True)),
            with_media=bool(raw.get("with_media", True)),
        )


@dataclass(frozen=True)
class PackageOffer:
    """What the source says it has packed, before a byte of it moves.

    The size is the whole point: it is what turns the copy into a percentage and a time estimate
    rather than a spinner. A source that cannot say how big it is sends 0, and the target shows a
    moving bar with no number instead of a made-up one.
    """

    id: str
    bytes: int = 0
    cards: int = 0
    notes: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "bytes": self.bytes,
                "cards": self.cards,
                "notes": self.notes,
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
        if not isinstance(raw, dict) or not str(raw.get("id", "")):
            raise PackageError("the other machine did not answer with a package")
        return cls(
            id=str(raw["id"]),
            bytes=_count(raw.get("bytes")),
            cards=_count(raw.get("cards")),
            notes=_count(raw.get("notes")),
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
