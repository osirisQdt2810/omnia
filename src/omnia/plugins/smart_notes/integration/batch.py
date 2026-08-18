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
    """One note's generation inputs, read on the main thread.

    The background op DOES now touch the collection: media results are materialized as they
    are produced (see GenerationService.generate_note), so add_media_file runs inside the
    QueryOp. That is deliberate — Anki hands the collection to a QueryOp's background thread
    and the backend serialises the write — and it is what lets a later tool read the
    reference the note will hold. These inputs are still read on the main thread."""

    nid: int
    config: SmartNotesNoteTypeConfig
    fields: dict[str, str]


# How many "<field> needs <prereq>" examples the summary tooltip names before it just counts.
_MAX_BLOCKED_EXAMPLES = 2


@dataclass
class BatchSummary:
    """Counts of how a batch resolved (for the closing summary tooltip)."""

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    # A few "<field> needs <prereq>" strings for the blocked fields, so the summary can say WHICH
    # field was blocked and by what (the count alone is not actionable). Bounded when rendered.
    blocked_examples: list[str] = field(default_factory=list)
    # Notes that WERE generatable but ended up with nothing generated (every field blocked,
    # skipped, or declined by every tool in its chain). Notes whose type has no config never get
    # here — they are dropped before generation — so this really means "we tried, and there was
    # nothing to make", which is what lets the integration gateway discard a clip that would
    # only ever hold the captured word.
    #
    # A note that produced nothing because something BROKE is excluded and listed in
    # ``errored_note_ids`` instead: breakage is transient (a provider outage, an expired key),
    # and throwing the user's capture away over it would lose work that one retry would recover.
    # That applies to a whole-note failure AND to a note whose every field errored — the latter
    # used to be discarded, which is the gap this split closes.
    empty_note_ids: list[int] = field(default_factory=list)
    # Notes kept for a retry: they generated nothing, but at least one field ERRORED, so
    # "nothing to make here" is not established. Never discarded.
    errored_note_ids: list[int] = field(default_factory=list)
    # Per-field generation errors across all notes (a single field raising), distinct from
    # ``failed`` (a whole note that could not be processed/written at all).
    field_failures: int = 0
    # Fields whose tool chain ran to the end and produced nothing WITHOUT anything breaking —
    # every tool simply declined. Counted apart from ``field_failures`` because nothing is
    # wrong: there was just nothing to make (a cloze whose word isn't in the sentence).
    unfilled: int = 0
    # Fields a NON-FIRST tool produced, i.e. the chain fell back. A deterministic first tool
    # that quietly stops matching would otherwise push every field to the (paid) LLM with no
    # sign of it anywhere but the log.
    tool_fallbacks: int = 0
    cancelled: bool = False

    def message(self) -> str:
        """Render the reference-style "Processed N, M failed, K skipped" summary."""
        parts = [f"Processed {self.processed} note(s)"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.blocked:
            # Name the first blocked field(s) and what they were waiting for: "1 blocked" alone
            # leaves the user with no idea which field, or that the fix is a config one (the
            # prerequisite field is usually just switched off).
            detail = "; ".join(self.blocked_examples[:_MAX_BLOCKED_EXAMPLES])
            suffix = f" ({detail})" if detail else ""
            parts.append(f"{self.blocked} blocked — missing prerequisites{suffix}")
        if self.field_failures:
            parts.append(f"{self.field_failures} field error(s)")
        if self.unfilled:
            parts.append(f"{self.unfilled} field(s) had no applicable tool")
        if self.tool_fallbacks:
            parts.append(f"{self.tool_fallbacks} field(s) fell back to a later tool")
        if self.errored_note_ids:
            # Says WHY a run that generated nothing still left notes behind — otherwise a user
            # with auto-discard on sees clips accumulating during an outage with no explanation.
            parts.append(f"kept {len(self.errored_note_ids)} note(s) for retry")
        prefix = "Cancelled — " if self.cancelled else ""
        return prefix + ", ".join(parts) + "."


def _unmaterialized(rule: Any, result: GenerationResult) -> str:
    """The fallback for an outcome built without its note's materializer.

    TEXT needs no materializer — no bytes, no media folder, nothing to name — so it is
    rendered here exactly as :func:`materialize` would. The first version of this raised for
    ANY kind, which turned a note whose only result was plain text into a counted failure
    written nowhere: precisely the "no output, no error" shape this whole change set out to
    remove, recreated one layer down.

    Media still raises, because a caller that produced bytes and carried no way to store them
    has a bug that silence would hide.
    """
    if result.kind == "text":
        return result.text or ""
    raise RuntimeError(
        f"no materializer for {getattr(rule, 'target_field', '?')!r}: an outcome carrying a "
        f"{result.kind} result must be built with the note's materializer"
    )


@dataclass
class _NoteOutcome:
    """The generated results for one note (or its failure), carried back to the main thread."""

    nid: int
    # The note's own memoised materializer, carried from generation to the write so both see
    # the SAME media filename. Defaults to a fresh one for the outcomes built in tests and on
    # the failure paths, which have no media to write.
    materialize: Callable[[SmartNotesFieldRule, GenerationResult], str] = field(
        default=_unmaterialized
    )
    results: list[tuple[SmartNotesFieldRule, GenerationResult]] = field(
        default_factory=list
    )
    blocked: int = 0
    blocked_examples: list[str] = field(default_factory=list)
    # Count of this note's fields whose generation raised and was isolated (siblings still ran).
    field_failures: int = 0
    # Count of this note's fields whose chain declined all the way through (nothing broke).
    unfilled: int = 0
    # Count of this note's fields a non-first tool in the chain produced.
    tool_fallbacks: int = 0
    failed: bool = False


class BatchGenerator:
    """Runs smart-notes generation over many notes with a cancellable progress dialog."""

    def __init__(
        self, service: GenerationService, settings: SmartNotesSettings
    ) -> None:
        self._service = service
        self._settings = settings

    def run(
        self,
        note_ids: list[int],
        on_done: Callable[[BatchSummary], None],
        *,
        show_progress: bool = True,
    ) -> None:
        """Generate smart fields for ``note_ids`` in the background, then call ``on_done``.

        Reads each note's fields + selects its enabled rules on the main thread, opens the
        progress dialog, then generates off-thread in chunks (honouring cancel), and finally
        writes results + reports a summary on the main thread.

        Args:
            note_ids: The notes to process (deduped here; cards of one note collapse to it).
            on_done: Main-thread callback receiving the :class:`BatchSummary`.
            show_progress: Open the modal progress dialog. Off for background auto-generation
                (the integration gateway) so a stream of clipped notes never stacks modal
                dialogs — which would freeze Anki; a summary tooltip still reports the result.
        """
        plans, deck_skipped = self._build_plans(dedupe_preserving_order(note_ids))
        if not plans:
            on_done(BatchSummary(skipped=deck_skipped))
            return

        total = len(plans)
        # Batch overwrite is driven by ``regenerate_when_batching``: when set, the batch
        # regenerates fields it already filled (ignoring per-field overwrite).
        force_overwrite = self._settings.regenerate_when_batching
        if show_progress:
            anki_compat.progress_start(f"Omnia: generating… (0/{total})", total)

        def op() -> tuple[list[_NoteOutcome], bool]:
            return self._generate(
                plans,
                total,
                force_overwrite=force_overwrite,
                show_progress=show_progress,
            )

        def on_success(result: tuple[list[_NoteOutcome], bool]) -> None:
            outcomes, cancelled = result
            summary = self._apply(outcomes)
            summary.skipped += deck_skipped
            summary.cancelled = cancelled
            if show_progress:
                anki_compat.progress_finish()
            on_done(summary)

        def on_failure(exc: Exception) -> None:
            if show_progress:
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
            try:
                note = anki_compat.get_note(nid)
            except Exception:
                # The note may have been deleted between selection/queueing and now (e.g. a clip
                # deleted during the gateway's debounce). Skip it rather than aborting the batch.
                logger.exception("smart_notes: batch skipping unreadable note %s", nid)
                continue
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
        self,
        plans: list[_NotePlan],
        total: int,
        *,
        force_overwrite: bool,
        show_progress: bool = True,
    ) -> tuple[list[_NoteOutcome], bool]:
        """Generate every plan in chunks off the main thread; returns outcomes + cancelled flag."""
        outcomes: list[_NoteOutcome] = []
        done = 0
        for batch in chunk(list(range(len(plans))), _CHUNK_SIZE):
            # want_cancel() is a simple thread-safe flag read on Anki's progress manager, so
            # it can be polled directly from this background thread between chunks. Only meaningful
            # when a progress dialog is shown (background auto-gen has none, so nothing to cancel).
            if show_progress and anki_compat.progress_was_cancelled():
                return outcomes, True
            for index in batch:
                plan = plans[index]
                outcomes.append(
                    self._generate_one(plan, force_overwrite=force_overwrite)
                )
            done += len(batch)
            if show_progress:
                anki_compat.run_on_main(
                    lambda d=done: anki_compat.progress_update(
                        f"Omnia: generating… ({d}/{total})", d, total
                    )
                )
        return outcomes, False

    def _generate_one(self, plan: _NotePlan, *, force_overwrite: bool) -> _NoteOutcome:
        try:
            # One materializer for the whole note, shared with _write_note below, so a
            # field's media is written once and the chain and the note agree on its name.
            materialize_once = note_materializer(plan.nid)
            results, blocked, failed = self._service.generate_note(
                plan.config,
                plan.fields,
                allow_empty_fields=self._settings.allow_empty_fields,
                force_overwrite=force_overwrite,
                materialize=materialize_once,
            )
            return _NoteOutcome(
                plan.nid,
                materialize=materialize_once,
                results=results,
                blocked=len(blocked),
                blocked_examples=[
                    f"{item.target_field} needs {', '.join(item.missing)}"
                    for item in blocked[:_MAX_BLOCKED_EXAMPLES]
                ],
                # A chain that ended empty-handed is only an ERROR when a tool actually broke;
                # "every tool declined" is its own, blameless outcome.
                field_failures=sum(1 for item in failed if item.kind == "error"),
                unfilled=sum(1 for item in failed if item.kind != "error"),
                tool_fallbacks=sum(
                    1 for rule, result in results if _fell_back(rule, result)
                ),
            )
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
            summary.blocked += outcome.blocked
            for example in outcome.blocked_examples:
                if (
                    example not in summary.blocked_examples
                    and len(summary.blocked_examples) < _MAX_BLOCKED_EXAMPLES
                ):
                    summary.blocked_examples.append(example)
            summary.field_failures += outcome.field_failures
            summary.unfilled += outcome.unfilled
            summary.tool_fallbacks += outcome.tool_fallbacks
            if not outcome.results:
                # A note with only blocked/errored/unfilled fields counts as such, not skipped
                # (skipped means there was genuinely nothing to generate).
                if (
                    not outcome.blocked
                    and not outcome.field_failures
                    and not outcome.unfilled
                ):
                    summary.skipped += 1
                if outcome.field_failures:
                    # Something broke, so we do NOT know there was nothing to make. Keep the
                    # note for a retry instead of offering it to the clip discarder.
                    summary.errored_note_ids.append(outcome.nid)
                else:
                    summary.empty_note_ids.append(outcome.nid)
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
                note[rule.target_field] = outcome.materialize(rule, result)
                wrote = True
            if wrote:
                anki_compat.update_note(note)
            return wrote
        except Exception:
            logger.exception("smart_notes: failed to write note %s", outcome.nid)
            return False


def _fell_back(rule: SmartNotesFieldRule, result: GenerationResult) -> bool:
    """Whether a NON-FIRST tool of ``rule``'s chain produced ``result``.

    Reads the provenance the pipeline stamps on the result (``GenerationResult.tool``) against
    the chain's first entry. An unstamped result (a rule generated outside the pipeline in a
    test) counts as no fallback.
    """
    first = rule.tools[0].name if rule.tools else ""
    return bool(result.tool) and result.tool != first


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


def note_materializer(nid: int) -> Callable[[Any, GenerationResult], str]:
    """Return a per-note :func:`materialize` that writes each field's media exactly ONCE.

    Two moments need the string a result becomes, and they are not the same moment. The
    generation chain needs it DURING the run — a tool reading an audio field must see the
    ``[sound:…]`` reference the note is going to hold, or it reads blank and the field is
    dropped before the tool is ever consulted. The writer needs it afterwards.

    Calling :func:`materialize` at both moments would add the same bytes to the media folder
    twice, and Anki renames on collision — so the second call would return a DIFFERENT filename
    from the one already handed downstream, and the extracted name would point at a file the
    note does not reference. Memoising per target field makes the two moments agree.
    """
    written: dict[str, str] = {}

    def materialize_once(rule: Any, result: GenerationResult) -> str:
        key = str(rule.target_field)
        if key not in written:
            written[key] = materialize(nid, rule, result)
        return written[key]

    return materialize_once


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
