"""The ONLY writer: persist a :class:`ChangePlan` to the collection, undo-safely.

A maintenance run can rewrite thousands of notes at once, so the write goes through Anki's
:class:`~aqt.operations.CollectionOp`: the whole batch lands in a single undo entry (Ctrl+Z
puts it all back) and the UI stays responsive. Nothing else in the plugin touches the
collection.

Collection access goes through :mod:`omnia.core.anki_compat` (the shared shim, whose Anki
imports are lazy) and ``aqt.operations`` is imported INSIDE :meth:`ChangeApplier.run`, so this
module imports headless and :meth:`ChangeApplier.write` can be unit-tested against a fake
collection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
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
        self._missing_note_ids: tuple[int, ...] = ()
        self._written_note_count = 0

    @property
    def missing_note_ids(self) -> tuple[int, ...]:
        """The planned notes that were gone by the time the write ran (deleted meanwhile).

        Empty until :meth:`write` has run; the caller reports it once the op finishes.
        """
        return self._missing_note_ids

    @property
    def written_note_count(self) -> int:
        """How many notes the last :meth:`write` actually submitted (0 before it runs)."""
        return self._written_note_count

    def run(self, parent: Any, on_done: Optional[Callable[[int], None]] = None) -> None:
        """Apply the plan in the background as one undoable operation.

        The write itself runs OFF the main thread (``CollectionOp.run_in_background``); only
        ``on_done`` is marshalled back to it.

        A no-op for an empty plan (``on_done`` still fires with 0, so a caller can report
        "nothing to do" without special-casing it).

        Args:
            parent: The Qt widget the operation reports progress/errors against.
            on_done: Called once the op succeeds, with the number of notes ACTUALLY written —
                the planned notes minus those the write had to skip (see
                :attr:`missing_note_ids`), so the user is never told about edits that did not
                happen.
        """
        if self._plan.is_empty:
            if on_done is not None:
                on_done(0)
            return
        from aqt.operations import CollectionOp

        op = CollectionOp(parent=parent, op=self.write)
        if on_done is not None:
            op = op.success(lambda _changes: on_done(self._written_note_count))
        op.run_in_background()

    def write(self, col: Any) -> Any:
        """Write every planned field into its note and save the batch.

        A note that no longer exists is SKIPPED, not fatal: the plan is a snapshot, and one
        note deleted since the preview must not cost the user the other 4 999 edits. The ids
        are recorded in :attr:`missing_note_ids` so the caller can report them.

        Args:
            col: The Anki collection (handed in by the ``CollectionOp``).

        Returns:
            The ``OpChanges`` from ``col.update_notes`` — what the op reports to Anki.
        """
        notes = []
        missing: list[int] = []
        for change in self._plan:
            note = anki_compat.get_note_or_none(change.note_id, col)
            if note is None:
                missing.append(change.note_id)
                continue
            names = set(note.keys())
            wrote = False
            for field, value in change.updates().items():
                # A field can vanish between planning and applying (the user edited the note
                # type). Skipping it is right — creating it would corrupt the note.
                if field not in names:
                    logger.warning(
                        "note %s has no field %r; skipping it", change.note_id, field
                    )
                    continue
                note[field] = value
                wrote = True
            # A note that gained nothing is left out: submitting it would bump its mod/usn
            # for no reason, marking it modified for the next AnkiWeb sync.
            if wrote:
                notes.append(note)
        self._missing_note_ids = tuple(missing)
        self._written_note_count = len(notes)
        if missing:
            logger.warning(
                "skipped %d note(s) deleted since the preview: %s",
                len(missing),
                missing,
            )
        return anki_compat.update_notes(notes, col)
