"""The ONLY writer: persist a :class:`ChangePlan` to the collection, undo-safely.

A maintenance run can rewrite thousands of notes at once, so the write goes through Anki's
:class:`~aqt.operations.CollectionOp`: the whole batch lands in a single undo entry (Ctrl+Z
puts it all back) and the UI stays responsive. Nothing else in the plugin touches the
collection.

``aqt``/``anki`` are imported INSIDE the methods, so this module imports headless and
:meth:`ChangeApplier.write` can be unit-tested against a fake collection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.plugins.note_maintenance.runner import ChangePlan

logger = get_logger("note_maintenance")


class ChangeApplier:
    """Writes one :class:`ChangePlan` back to the collection inside a ``CollectionOp``."""

    def __init__(self, plan: ChangePlan) -> None:
        """Initialise the applier.

        Args:
            plan: The reviewed, confirmed plan to persist.
        """
        self._plan = plan

    def run(self, parent: Any, on_done: Optional[Callable[[int], None]] = None) -> None:
        """Apply the plan on the main thread as one undoable operation.

        A no-op for an empty plan (``on_done`` still fires with 0, so a caller can report
        "nothing to do" without special-casing it).

        Args:
            parent: The Qt widget the operation reports progress/errors against.
            on_done: Called with the number of notes written once the op succeeds.
        """
        if self._plan.is_empty:
            if on_done is not None:
                on_done(0)
            return
        from aqt.operations import CollectionOp

        written = self._plan.note_count
        op = CollectionOp(parent=parent, op=self.write)
        if on_done is not None:
            op = op.success(lambda _changes: on_done(written))
        op.run_in_background()

    def write(self, col: Any) -> Any:
        """Write every planned field into its note and save the batch.

        Args:
            col: The Anki collection (handed in by the ``CollectionOp``).

        Returns:
            The ``OpChanges`` from ``col.update_notes`` — what the op reports to Anki.
        """
        notes = []
        for change in self._plan:
            note = col.get_note(change.note_id)
            names = set(note.keys())
            for field, value in change.updates().items():
                # A field can vanish between planning and applying (the user edited the note
                # type). Skipping it is right — creating it would corrupt the note.
                if field not in names:
                    logger.warning(
                        "note_maintenance: note %s has no field %r; skipping it",
                        change.note_id,
                        field,
                    )
                    continue
                note[field] = value
            notes.append(note)
        return col.update_notes(notes)
