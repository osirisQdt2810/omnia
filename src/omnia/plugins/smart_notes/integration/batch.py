"""Cancellable, counted batch generation for smart_notes (Browser + sidebar batches).

Ports the reference add-on's ``process_cards_with_progress``: generate smart fields for many
notes off the Qt main thread behind a cancellable progress dialog ("Generating (n/total)"),
in chunks so a long run can be cancelled mid-flight and the provider isn't hit all at once.
Per-note generation goes through :meth:`GenerationService.generate_note`, so chained fields,
skip rules, and Markdown conversion all apply. Results are written back on the main thread.

The pure planning/selection logic lives in ``engine``; this module is the Anki glue tying
that to the threading + progress + media-write seams in ``core/anki_compat``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine import (
    GenerationResult,
    GenerationService,
    applies_to_deck,
    chunk,
    dedupe_preserving_order,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
        SmartNotesSettings,
    )

logger = get_logger("smart_notes")

# Notes generated per chunk before the progress bar updates / a cancel is honoured. Small so a
# cancel feels responsive without flooding the provider; mirrors the reference's batching.
_CHUNK_SIZE = 5


@dataclass
class _NotePlan:
    """One note's generation inputs, read on the main thread (the bg op never touches the col)."""

    nid: int
    config: SmartNotesNoteTypeConfig
    fields: dict[str, str]


@dataclass
class BatchSummary:
    """Counts of how a batch resolved (for the closing summary tooltip)."""

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False

    def message(self) -> str:
        """Render the reference-style "Processed N, M failed, K skipped" summary."""
        parts = [f"Processed {self.processed} note(s)"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        prefix = "Cancelled — " if self.cancelled else ""
        return prefix + ", ".join(parts) + "."


@dataclass
class _NoteOutcome:
    """The generated results for one note (or its failure), carried back to the main thread."""

    nid: int
    results: list[tuple[SmartNotesFieldRule, GenerationResult]] = field(
        default_factory=list
    )
    failed: bool = False


class BatchGenerator:
    """Runs smart-notes generation over many notes with a cancellable progress dialog."""

    def __init__(
        self, service: GenerationService, settings: SmartNotesSettings
    ) -> None:
        self._service = service
        self._settings = settings

    def run(self, note_ids: list[int], on_done: Callable[[BatchSummary], None]) -> None:
        """Generate smart fields for ``note_ids`` in the background, then call ``on_done``.

        Reads each note's fields + selects its enabled rules on the main thread, opens the
        progress dialog, then generates off-thread in chunks (honouring cancel), and finally
        writes results + reports a summary on the main thread.

        Args:
            note_ids: The notes to process (deduped here; cards of one note collapse to it).
            on_done: Main-thread callback receiving the :class:`BatchSummary`.
        """
        plans, deck_skipped = self._build_plans(dedupe_preserving_order(note_ids))
        if not plans:
            on_done(BatchSummary(skipped=deck_skipped))
            return

        total = len(plans)
        # Batch overwrite is driven by ``regenerate_when_batching``: when set, the batch
        # regenerates fields it already filled (ignoring per-field overwrite).
        force_overwrite = self._settings.regenerate_when_batching
        anki_compat.progress_start(f"Omnia: generating… (0/{total})", total)

        def op() -> tuple[list[_NoteOutcome], bool]:
            return self._generate(plans, total, force_overwrite=force_overwrite)

        def on_success(result: tuple[list[_NoteOutcome], bool]) -> None:
            outcomes, cancelled = result
            summary = self._apply(outcomes)
            summary.skipped += deck_skipped
            summary.cancelled = cancelled
            anki_compat.progress_finish()
            on_done(summary)

        def on_failure(exc: Exception) -> None:
            anki_compat.progress_finish()
            logger.exception("smart_notes batch failed")
            on_done(BatchSummary(failed=total))

        anki_compat.run_in_background(op, on_success=on_success, on_failure=on_failure)

    def _build_plans(self, note_ids: list[int]) -> tuple[list[_NotePlan], int]:
        """Select the generatable plans; return ``(plans, deck_skipped)``.

        A note with no config / no generatable field is dropped silently. A note whose config
        is deck-scoped and matches NONE of the note's card decks is counted as skipped (it is
        configured + generatable, just out of this config's deck scope).
        """
        plans: list[_NotePlan] = []
        deck_skipped = 0
        for nid in note_ids:
            note = anki_compat.get_note(nid)
            config = self._settings.note_type_config(_note_type_name(note))
            if config is None or not config.generatable_fields():
                continue
            if config.decks and not any(
                applies_to_deck(config, did) for did in anki_compat.note_deck_ids(note)
            ):
                deck_skipped += 1
                continue
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
            plans.append(_NotePlan(nid, config, fields))
        return plans, deck_skipped

    def _generate(
        self, plans: list[_NotePlan], total: int, *, force_overwrite: bool
    ) -> tuple[list[_NoteOutcome], bool]:
        """Generate every plan in chunks off the main thread; returns outcomes + cancelled flag."""
        outcomes: list[_NoteOutcome] = []
        done = 0
        for batch in chunk(list(range(len(plans))), _CHUNK_SIZE):
            # want_cancel() is a simple thread-safe flag read on Anki's progress manager, so
            # it can be polled directly from this background thread between chunks.
            if anki_compat.progress_was_cancelled():
                return outcomes, True
            for index in batch:
                plan = plans[index]
                outcomes.append(
                    self._generate_one(plan, force_overwrite=force_overwrite)
                )
            done += len(batch)
            anki_compat.run_on_main(
                lambda d=done: anki_compat.progress_update(
                    f"Omnia: generating… ({d}/{total})", d, total
                )
            )
        return outcomes, False

    def _generate_one(self, plan: _NotePlan, *, force_overwrite: bool) -> _NoteOutcome:
        try:
            results = self._service.generate_note(
                plan.config,
                plan.fields,
                allow_empty_fields=self._settings.allow_empty_fields,
                force_overwrite=force_overwrite,
            )
            return _NoteOutcome(plan.nid, results=results)
        except Exception:  # one bad note must not abort the rest of the batch
            logger.exception("smart_notes: failed to generate note %s", plan.nid)
            return _NoteOutcome(plan.nid, failed=True)

    def _apply(self, outcomes: list[_NoteOutcome]) -> BatchSummary:
        """Write generated content back to the notes + media (main thread); count outcomes."""
        summary = BatchSummary()
        for outcome in outcomes:
            if outcome.failed:
                summary.failed += 1
                continue
            if not outcome.results:
                summary.skipped += 1
                continue
            if self._write_note(outcome):
                summary.processed += 1
            else:
                summary.failed += 1
        return summary

    def _write_note(self, outcome: _NoteOutcome) -> bool:
        try:
            note = anki_compat.get_note(outcome.nid)
            wrote = False
            for rule, result in outcome.results:
                if rule.target_field not in note:
                    continue
                note[rule.target_field] = materialize(outcome.nid, rule, result)
                wrote = True
            if wrote:
                anki_compat.update_note(note)
            return wrote
        except Exception:
            logger.exception("smart_notes: failed to write note %s", outcome.nid)
            return False


def materialize(nid: int, rule: Any, result: GenerationResult) -> str:
    """Turn a :class:`GenerationResult` into the string written into a note field.

    Text is the rendered HTML; image/tts write the bytes to media and return the embed tag.
    Shared by the batch runner, the editor button, and review-time generation so all three
    embed media identically.
    """
    if result.kind == "text":
        return result.text or ""
    filename = f"omnia-{nid}-{rule.target_field}.{result.ext}"
    stored = anki_compat.add_media_file(filename, result.data or b"")
    if result.kind == "image":
        return f'<img src="{stored}">'
    return f"[sound:{stored}]"  # tts


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
