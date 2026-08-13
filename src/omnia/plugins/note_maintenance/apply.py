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
from dataclasses import dataclass
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.plugins.note_maintenance.runner import ChangePlan

logger = get_logger("note_maintenance")


@dataclass(frozen=True)
class ApplyOutcome:
    """What one :meth:`ChangeApplier.write` actually did — counts AND how to say them.

    The write is the only place that knows a planned note was skipped, so the phrasing lives
    with the numbers instead of being reinvented by whoever shows it: a caller that formats a
    bare count of its own reports "N notes updated" while silently dropping the skips, which
    is precisely the reassurance this feature must not give.

    Attributes:
        written_note_count: How many notes were submitted to the collection.
        missing_note_ids: Planned notes that were gone by the time the write ran (deleted
            since the preview).
        stale_note_ids: Planned notes whose text had been edited since the preview; their
            changed fields were left ALONE, so the user's own edit wins.
    """

    written_note_count: int = 0
    missing_note_ids: tuple[int, ...] = ()
    stale_note_ids: tuple[int, ...] = ()

    @property
    def message(self) -> str:
        """One line for the user: what was written, and what was skipped instead."""
        parts = [
            f"Omnia: {self.written_note_count} note(s) updated — Ctrl+Z undoes the batch."
        ]
        if self.missing_note_ids:
            parts.append(
                f"{len(self.missing_note_ids)} note(s) had been deleted — skipped."
            )
        if self.stale_note_ids:
            parts.append(
                f"{len(self.stale_note_ids)} note(s) changed since the preview — left alone."
            )
        return " ".join(parts)


class ChangeApplier:
    """Writes one :class:`ChangePlan` back to the collection inside a ``CollectionOp``."""

    def __init__(self, plan: ChangePlan) -> None:
        """Initialise the applier.

        Args:
            plan: The reviewed, confirmed plan to persist.
        """
        self._plan = plan
        self._outcome = ApplyOutcome()

    @property
    def outcome(self) -> ApplyOutcome:
        """What the last :meth:`write` did (an empty outcome before it has run)."""
        return self._outcome

    def run(
        self, parent: Any, on_done: Optional[Callable[[ApplyOutcome], None]] = None
    ) -> None:
        """Apply the plan in the background as one undoable operation.

        The write itself runs OFF the main thread (``CollectionOp.run_in_background``); only
        ``on_done`` is marshalled back to it.

        A no-op for an empty plan (``on_done`` still fires with an empty outcome, so a caller
        can report "nothing to do" without special-casing it).

        Args:
            parent: The Qt widget the operation reports progress/errors against.
            on_done: Called once the op succeeds, with the :class:`ApplyOutcome` — the notes
                ACTUALLY written plus the ones the write had to skip, so the user is never
                told about edits that did not happen.
        """
        if self._plan.is_empty:
            if on_done is not None:
                on_done(self._outcome)
            return
        from aqt.operations import CollectionOp

        op = CollectionOp(parent=parent, op=self.write)
        if on_done is not None:
            op = op.success(lambda _changes: on_done(self._outcome))
        op.run_in_background()

    def write(self, col: Any) -> Any:
        """Write every planned field into its note and save the batch.

        The plan is a SNAPSHOT, and the collection can move under it between the preview and
        the confirmation. Two cases are skipped rather than treated as fatal, because one note
        must not cost the user the other 4 999 edits:

        * the note is gone (deleted meanwhile) — recorded in the :attr:`outcome`;
        * the field no longer holds the ``before`` the user reviewed (they edited the note, or
          another add-on did) — recorded there too. Writing the planned value there would
          silently revert an edit the user never saw in this diff.

        Args:
            col: The Anki collection (handed in by the ``CollectionOp``).

        Returns:
            The ``OpChanges`` from ``col.update_notes`` — what the op reports to Anki.
        """
        notes = []
        missing: list[int] = []
        stale: list[int] = []
        for change in self._plan:
            note = anki_compat.get_note_or_none(change.note_id, col)
            if note is None:
                missing.append(change.note_id)
                continue
            names = set(note.keys())
            wrote = False
            for field_change in change.fields:
                field = field_change.field
                # A field can vanish between planning and applying (the user edited the note
                # type). Skipping it is right — creating it would corrupt the note.
                if field not in names:
                    logger.warning(
                        "note %s has no field %r; skipping it", change.note_id, field
                    )
                    continue
                if str(note[field]) != field_change.before:
                    logger.warning(
                        "note %s field %r changed since the preview; leaving it alone",
                        change.note_id,
                        field,
                    )
                    stale.append(change.note_id)
                    continue
                note[field] = field_change.after
                wrote = True
            # A note that gained nothing is left out: submitting it would bump its mod/usn
            # for no reason, marking it modified for the next AnkiWeb sync.
            if wrote:
                notes.append(note)
        self._outcome = ApplyOutcome(
            written_note_count=len(notes),
            missing_note_ids=tuple(missing),
            stale_note_ids=tuple(dict.fromkeys(stale)),  # de-duped, in plan order
        )
        if missing:
            logger.warning(
                "skipped %d note(s) deleted since the preview: %s",
                len(missing),
                missing,
            )
        return anki_compat.update_notes(notes, col)
