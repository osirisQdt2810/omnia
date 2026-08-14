"""Run maintenance tasks over notes and produce a :class:`ChangePlan` — a PURE, read-only step.

The runner never writes. It reads :class:`~omnia.plugins.note_maintenance.base.NoteView`
snapshots, applies the enabled tasks in ``order``, and returns what WOULD change, so the user
can review a diff before anything touches the collection (``apply.py`` is the only writer).

Tasks compose: each one sees the note as the previous tasks left it, so two tasks writing the
same field layer in ``order`` (the last one wins). A field the tasks end up restoring to its
original value produces no change at all, and a field the note type does not have is dropped —
a task may reshape existing text, never add a field the note lacks.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Optional

from omnia.plugins.note_maintenance.base import MaintenanceTask, NoteView


@dataclass(frozen=True)
class FieldChange:
    """One field's before/after — the unit the preview diff renders."""

    field: str
    before: str
    after: str


@dataclass(frozen=True)
class NoteChange:
    """Everything one note would change, as a set of :class:`FieldChange`s."""

    note_id: int
    fields: tuple[FieldChange, ...]


class SkipReason(enum.Enum):
    """Why a note's own note type contributed nothing to a run.

    A selection spans whatever note types the user highlighted, and settings are per note type
    — so passing a note over is NORMAL, not an error. It must still be said out loud: "nothing
    happened" and "nothing happened because this note type is not set up" look identical to a
    user, and the second one is the whole reason the feature seemed broken.
    """

    #: No settings exist for this note type (or the stored entry is not a table).
    UNCONFIGURED = "unconfigured"
    #: Settings exist, but the user unticked the note type.
    DISABLED = "disabled"
    #: The note type is ticked, but every task inside it is switched off.
    NO_TASKS = "no_tasks"

    @property
    def explanation(self) -> str:
        """A phrase completing "N note(s) of 'X' — …" for the user."""
        return _SKIP_EXPLANATIONS[self]


_SKIP_EXPLANATIONS = {
    SkipReason.UNCONFIGURED: "no maintenance settings for this note type",
    SkipReason.DISABLED: "this note type is switched off",
    SkipReason.NO_TASKS: "every task is switched off for this note type",
}


@dataclass(frozen=True)
class SkippedNotes:
    """The notes of ONE note type a run passed over, and why."""

    note_type: str
    reason: SkipReason
    note_count: int

    @property
    def label(self) -> str:
        """The note type as the user should read it (a note with no resolvable type included)."""
        return self.note_type or "(unknown note type)"

    def describe(self) -> str:
        """One phrase for the preview: how many notes, of what, and why they were passed over."""
        return (
            f"{self.note_count} note(s) of '{self.label}' — {self.reason.explanation}"
        )


@dataclass(frozen=True)
class ChangePlan:
    """The full, reviewable result of a run: the notes that would change, and how.

    Empty by construction when nothing matched, so a caller can gate the preview dialog on
    :attr:`is_empty` instead of counting entries itself.

    :attr:`skipped` carries the OTHER half of the answer — the selected notes the run could
    not act on, grouped by note type. It defaults to empty, so a single-note-type run (or a
    plan rebuilt from a preview selection) constructs exactly as before.
    """

    notes: tuple[NoteChange, ...] = ()
    skipped: tuple[SkippedNotes, ...] = ()

    def __iter__(self) -> Iterator[NoteChange]:
        return iter(self.notes)

    @property
    def is_empty(self) -> bool:
        """True when no note would change."""
        return not self.notes

    @property
    def note_count(self) -> int:
        """How many notes would change."""
        return len(self.notes)

    @property
    def field_count(self) -> int:
        """How many fields would change across every note."""
        return sum(len(note.fields) for note in self.notes)

    @property
    def skipped_note_count(self) -> int:
        """How many selected notes the run passed over."""
        return sum(entry.note_count for entry in self.skipped)

    @property
    def skip_summary(self) -> str:
        """What the run passed over, as one line ("" when it passed over nothing)."""
        if not self.skipped:
            return ""
        return "Skipped " + "; ".join(entry.describe() for entry in self.skipped) + "."


class MaintenanceRunner:
    """Applies an ordered set of tasks to notes and reports what would change."""

    def __init__(self, tasks: Sequence[MaintenanceTask]) -> None:
        """Initialise the runner.

        Args:
            tasks: The candidate tasks. Disabled ones are skipped and the rest run in
                ``order``; the caller does not pre-filter or pre-sort.
        """
        self._tasks = tuple(tasks)
        # Settled once: the task set is fixed at construction, and a mixed-note-type run asks
        # for it per note (see NoteTypePlanner).
        enabled = [task for task in self._tasks if task.is_enabled]
        self._active = tuple(sorted(enabled, key=lambda task: task.order))

    @property
    def active_tasks(self) -> tuple[MaintenanceTask, ...]:
        """The enabled tasks in run order (ties keep the order they were given in)."""
        return self._active

    def plan(self, notes: Iterable[NoteView]) -> ChangePlan:
        """Return the :class:`ChangePlan` for ``notes`` without writing anything.

        Args:
            notes: The note snapshots to run the tasks over.

        Returns:
            A plan holding one :class:`NoteChange` per note that would actually change.
        """
        changes = (self.plan_note(note) for note in notes)
        return ChangePlan(tuple(change for change in changes if change is not None))

    def plan_note(self, note: NoteView) -> Optional[NoteChange]:
        """Return what this runner's tasks would change on ONE note, or None if nothing.

        The per-note entry point, so a run spanning several note types can plan each note with
        its own note type's runner while keeping the selection's order (see
        :class:`~omnia.plugins.note_maintenance.note_types.NoteTypePlanner`).
        """
        return self._plan_note(note, self._active)

    @staticmethod
    def _plan_note(
        note: NoteView, tasks: Sequence[MaintenanceTask]
    ) -> Optional[NoteChange]:
        """Run ``tasks`` over one note; return its :class:`NoteChange`, or None if unchanged.

        A task naming a field this note type does not have is filtered out HERE, where
        ``note.fields`` is the authoritative field list: a task may only rewrite text the note
        already holds, never conjure a field (the apply step cannot create one either, so
        planning it would show the user a change that never happens).
        """
        current = note
        for task in tasks:
            updates = {
                name: value
                for name, value in task.process(current).items()
                if name in note.fields
            }
            if updates:
                current = current.with_updates(updates)
        changes = tuple(
            FieldChange(field=name, before=note.field(name), after=value)
            for name, value in current.fields.items()
            if value != note.field(name)
        )
        return NoteChange(note_id=note.note_id, fields=changes) if changes else None
