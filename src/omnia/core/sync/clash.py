"""What a pull would land on top of, worked out before anything moves.

A deck or a note type with the same NAME already here is not an error and not usually a problem —
two machines syncing the same collection through AnkiWeb share most of both — but it is the one
thing the user has to be told before the copy runs, because it is the only way an arriving
package can change something they already had.

The two clash for different reasons, and the sentences say which:

* **A deck** with the same name receives the arriving cards. Nothing of theirs is removed; the
  deck simply holds more afterwards. That is almost always what was wanted, and it is still worth
  saying out loud, because "Japanese" here and "Japanese" there can be two different decks that
  happen to share a word.
* **A note type** with the same name is the sharp one. If the fields differ — a field added on
  this machine, a template edited here — then what arrives and what is here are not the same
  thing wearing one name, and merging them is a decision rather than a detail.

What happens to a note that exists on BOTH machines is the user's to choose (:data:`KEEP` or
:data:`OVERRIDE`), and this module defines the choice rather than making it.

Pure: it compares two inventories and returns what it found.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: Leave what is here alone; an arriving note that already exists is skipped. The default,
#: because a copy that silently replaced an edit made on this machine is the one outcome nobody
#: asks for and nobody notices until much later.
KEEP = "keep"
#: Replace what is here with what arrives.
OVERRIDE = "override"

POLICIES = (KEEP, OVERRIDE)


@dataclass(frozen=True)
class NoteTypeClash:
    """A note type that exists on both machines."""

    name: str
    #: Whether the FIELDS differ. Same name, same fields is a note type two machines share; same
    #: name, different fields is two different things that happen to be called one word, and
    #: merging them is what empties a field on every note that used it.
    fields_differ: bool = False
    here: tuple[str, ...] = ()
    there: tuple[str, ...] = ()


@dataclass(frozen=True)
class Clashes:
    """Everything a pull would land on top of."""

    decks: tuple[str, ...] = ()
    note_types: tuple[NoteTypeClash, ...] = ()

    def __bool__(self) -> bool:
        """Whether there is anything worth stopping to say."""
        return bool(self.decks or self.note_types)

    @property
    def serious(self) -> tuple[NoteTypeClash, ...]:
        """The note types whose fields do not match — the ones that can lose something."""
        return tuple(clash for clash in self.note_types if clash.fields_differ)


def find_clashes(
    *,
    decks: Iterable[str],
    note_types: Iterable[str],
    here: Any,
    there: Any,
) -> Clashes:
    """What the chosen decks and note types would collide with on THIS machine.

    Args:
        decks: The deck names about to be pulled.
        note_types: The note type names about to be pulled.
        here: This machine's inventory.
        there: The other machine's inventory, for the field lists to compare against.

    Returns:
        What already exists here, by name.
    """
    local_decks = {entry.name for entry in getattr(here, "decks", ())}
    local_types = {
        entry.name: tuple(entry.fields) for entry in getattr(here, "note_types", ())
    }
    remote_types = {
        entry.name: tuple(entry.fields) for entry in getattr(there, "note_types", ())
    }
    return Clashes(
        decks=tuple(sorted(name for name in decks if name in local_decks)),
        note_types=tuple(
            NoteTypeClash(
                name=name,
                fields_differ=local_types[name] != remote_types.get(name, ()),
                here=local_types[name],
                there=remote_types.get(name, ()),
            )
            for name in sorted(note_types)
            if name in local_types
        ),
    )


def describe(clashes: Clashes, policy: str) -> list[str]:
    """The sentences to show before a pull that would land on something.

    One per kind, written so the user can decide without opening anything else. The list is
    ordered by how much is at stake: mismatched note types first, because they are the only ones
    where something already here can be changed.
    """
    lines: list[str] = []
    serious = clashes.serious
    if serious:
        names = ", ".join(clash.name for clash in serious[:4])
        more = "" if len(serious) <= 4 else f" and {len(serious) - 4} more"
        lines.append(
            f"{names}{more} — this computer has a note type with that name and DIFFERENT "
            "fields. Anki will merge them, which can leave a field empty on notes that used it."
        )
    shared = tuple(clash for clash in clashes.note_types if not clash.fields_differ)
    if shared:
        lines.append(
            f"{len(shared)} note type"
            + ("" if len(shared) == 1 else "s")
            + " already here with the same fields — those will just be reused."
        )
    if clashes.decks:
        names = ", ".join(clashes.decks[:4])
        more = "" if len(clashes.decks) <= 4 else f" and {len(clashes.decks) - 4} more"
        lines.append(
            f"{names}{more} — already here. The arriving cards go into the same deck; nothing "
            "in it is removed."
        )
    lines.append(
        "A note that exists on both computers will be left as it is here."
        if policy == KEEP
        else "A note that exists on both computers will be REPLACED by the arriving one."
    )
    return lines


def normalise(policy: Any) -> str:
    """A stored policy, or the safe one.

    Unknown values fall back to :data:`KEEP` rather than to whatever was written: a hand-edited
    config with a typo in it must not be read as permission to overwrite.
    """
    text = str(policy or "").strip().lower()
    return text if text in POLICIES else KEEP
