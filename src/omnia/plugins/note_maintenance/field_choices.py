"""What a note-field dropdown offers, including the value the note type no longer has.

A task option that names a field used to be free text: nothing said which fields exist, and a
typo failed silently at run time (the task simply found nothing). The settings panel offers the
selected note type's REAL fields instead — which raises the question this module answers: what
happens to a stored value that is not among them?

It is kept, and marked. A note type gets edited — a field renamed, a field removed — while a
task still names the old one; and the same map is synced between devices whose note types are
not identical. Dropping the value would rewrite the user's setting to whatever landed at index
0 the next time they pressed Save, which is exactly the silent data loss this plugin has
already shipped three of. So the stored value is ALWAYS one of the entries: a real field where
the note type still has it, a stale one (kept, labelled) where it does not.

Pure module — no ``aqt``/``anki``, no Qt: which entries exist is a decision about the user's
data, and the widget that shows them is thin glue over this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Appended to a stored value the note type does not have, so the row reads as a warning.
STALE_SUFFIX = " — not in this note type"
#: Shown for an empty value on an option that has no meaningful blank of its own.
UNSET_LABEL = "(not set)"


@dataclass(frozen=True)
class FieldChoice:
    """One entry of a field dropdown.

    Attributes:
        value: What is STORED when this entry is picked (never the label).
        label: What the user reads.
        is_stale: True when this is a stored value the note type no longer has — kept so a
            save cannot silently replace it, and marked so the user can see why it is odd.
    """

    value: str
    label: str
    is_stale: bool = False


class FieldChoices:
    """The entries a field dropdown offers for ONE note type."""

    def __init__(self, fields: Sequence[str], *, blank_label: str = "") -> None:
        """Initialise the choices.

        Args:
            fields: The note type's field names, in the note type's own order. Empty for a note
                type this collection does not have — every stored value is then stale, which is
                the honest answer.
            blank_label: What an empty value MEANS for this option, when it means something
                (a field map's target: "rewrite the source in place"). Left empty, no blank
                entry is offered — except when blank is what is stored, which is still kept.
        """
        self._fields = tuple(fields)
        self._blank_label = blank_label

    def entries(self, value: str) -> tuple[FieldChoice, ...]:
        """Return the dropdown entries with ``value`` guaranteed to be one of them.

        Args:
            value: The option's stored value (``""`` when it holds no field).

        Returns:
            The blank entry (when the option has one, or when blank is what is stored), then
            the note type's fields in their own order, then ``value`` as a stale entry when the
            note type does not have it.
        """
        entries: list[FieldChoice] = []
        if self._blank_label or not value:
            entries.append(FieldChoice("", self._blank_label or UNSET_LABEL))
        entries.extend(FieldChoice(name, name) for name in self._fields)
        if value and value not in self._fields:
            entries.append(FieldChoice(value, f"{value}{STALE_SUFFIX}", is_stale=True))
        return tuple(entries)
