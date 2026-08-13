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

    def updates(self) -> dict[str, str]:
        """Return ``{field: new value}`` — what the apply step writes back."""
        return {change.field: change.after for change in self.fields}


@dataclass(frozen=True)
class ChangePlan:
    """The full, reviewable result of a run: the notes that would change, and how.

    Empty by construction when nothing matched, so a caller can gate the preview dialog on
    :attr:`is_empty` instead of counting entries itself.
    """

    notes: tuple[NoteChange, ...] = ()

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


class MaintenanceRunner:
    """Applies an ordered set of tasks to notes and reports what would change."""

    def __init__(self, tasks: Sequence[MaintenanceTask]) -> None:
        """Initialise the runner.

        Args:
            tasks: The candidate tasks. Disabled ones are skipped and the rest run in
                ``order``; the caller does not pre-filter or pre-sort.
        """
        self._tasks = tuple(tasks)

    @property
    def active_tasks(self) -> tuple[MaintenanceTask, ...]:
        """The enabled tasks in run order (ties keep the order they were given in)."""
        enabled = [task for task in self._tasks if task.is_enabled]
        return tuple(sorted(enabled, key=lambda task: task.order))

    def plan(self, notes: Iterable[NoteView]) -> ChangePlan:
        """Return the :class:`ChangePlan` for ``notes`` without writing anything.

        Args:
            notes: The note snapshots to run the tasks over.

        Returns:
            A plan holding one :class:`NoteChange` per note that would actually change.
        """
        tasks = self.active_tasks
        changes = [self._plan_note(note, tasks) for note in notes]
        return ChangePlan(tuple(change for change in changes if change is not None))

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
