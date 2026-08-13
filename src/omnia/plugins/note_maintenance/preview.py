"""The reviewable selection over a :class:`ChangePlan` — what the preview shows and keeps.

A maintenance run is destructive, so the plan is a PROPOSAL: the user sees every pending change
as a diff and unticks the ones they would rather keep. That decision is state, and it belongs
with the plan rather than with the Qt widgets that display it — so the dialog
(:mod:`omnia.gui.note_maintenance.preview_dialog`) is thin glue that renders :attr:`NotePreview.rows`
and flips inclusion flags, while this module owns which changes survive and rebuilds the
(smaller) plan the apply step writes.

Everything starts included: what the user reviewed is what Apply writes unless they say
otherwise.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

from omnia.plugins.note_maintenance.diff import DiffRow, NoteDiff
from omnia.plugins.note_maintenance.runner import ChangePlan, NoteChange


class NotePreview:
    """One note's diff rows plus which of its fields the user has kept."""

    def __init__(self, change: NoteChange) -> None:
        """Initialise the preview.

        Args:
            change: The note's pending changes, as produced by the runner.
        """
        self._change = change
        self._rows = tuple(NoteDiff(change).rows())
        self._excluded: set[str] = set()

    @property
    def note_id(self) -> int:
        """The id of the note being previewed."""
        return self._change.note_id

    @property
    def rows(self) -> tuple[DiffRow, ...]:
        """The field diffs to display, in the note type's field order."""
        return self._rows

    def is_included(self, field: str) -> bool:
        """Whether ``field``'s change would be written."""
        return field not in self._excluded

    def set_included(self, field: str, included: bool) -> None:
        """Include or exclude ``field``'s change."""
        if included:
            self._excluded.discard(field)
        else:
            self._excluded.add(field)

    def include_all(self, included: bool) -> None:
        """Include or exclude every field of this note at once (the note-level tick)."""
        self._excluded = set() if included else {row.field for row in self._rows}

    @property
    def included_fields(self) -> tuple[str, ...]:
        """The fields that would be written, in display order."""
        return tuple(row.field for row in self._rows if self.is_included(row.field))

    @property
    def is_fully_included(self) -> bool:
        """True when every displayed field is still ticked."""
        return len(self.included_fields) == len(self._rows)

    @property
    def is_partly_included(self) -> bool:
        """True when SOME but not all fields are ticked (the note tick shows as partial)."""
        return 0 < len(self.included_fields) < len(self._rows)

    def selection(self) -> Optional[NoteChange]:
        """Return this note's change limited to its included fields, or None if none are.

        Only fields that were actually DISPLAYED can be selected: a change the diff dropped
        (because the value did not really differ) was never offered to the user, so it is not
        written either.
        """
        included = set(self.included_fields)
        fields = tuple(
            change for change in self._change.fields if change.field in included
        )
        return NoteChange(note_id=self.note_id, fields=fields) if fields else None


class PreviewModel:
    """The whole reviewable plan: one :class:`NotePreview` per note that would change."""

    def __init__(self, plan: ChangePlan) -> None:
        """Initialise the model.

        Args:
            plan: The runner's plan. A note whose every change turned out to be a no-op renders
                no rows, so it is dropped here rather than shown as an empty group.
        """
        previews = (NotePreview(change) for change in plan)
        self._notes = tuple(preview for preview in previews if preview.rows)

    def __iter__(self) -> Iterator[NotePreview]:
        return iter(self._notes)

    @property
    def notes(self) -> tuple[NotePreview, ...]:
        """The per-note previews, in plan order."""
        return self._notes

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to review."""
        return not self._notes

    @property
    def selected_note_count(self) -> int:
        """How many notes would be written with the current ticks."""
        return sum(1 for note in self._notes if note.included_fields)

    @property
    def selected_field_count(self) -> int:
        """How many fields would be written with the current ticks."""
        return sum(len(note.included_fields) for note in self._notes)

    def selected_plan(self) -> ChangePlan:
        """Return the plan holding ONLY the ticked changes (what Apply writes)."""
        selections = (note.selection() for note in self._notes)
        return ChangePlan(
            tuple(selection for selection in selections if selection is not None)
        )
