"""Cancellable, counted batch generation for smart_notes (Browser + sidebar batches).

Ports the reference add-on's ``process_cards_with_progress``: generate smart fields for many
notes off the Qt main thread behind a cancellable progress dialog ("Generating (n/total)").
Per-note semantics — the dependency order, the block gate, the skip predicate, the chaining —
belong to :class:`~omnia.plugins.smart_notes.engine.note_run.NoteRun`; this module drives
several of those at once so the provider is not left idle between one note's round trips.

The shape of a run is **cohort → round → wave**:

* a **cohort** is up to N notes sharing one note-type config, so their dependency levels line
  up (in the real collections this feature targets, every note of a type has the same shape);
* a **round** advances every unfinished run in the cohort by exactly one dependency level;
* the **wave** is that round's field work from every note, dispatched together — and, when
  K-note batching is on, planned so that several notes' copies of the SAME field travel as one
  provider call (:mod:`~omnia.plugins.smart_notes.engine.batching`).

Everything except the wave runs on this one background (driver) thread: the gates, the commit,
and therefore every ``materialize`` — which is what keeps media writes single-threaded and
keeps a media write off the pool. Results are written back to notes on the main thread, as
before. A cancel is honoured between COHORTS, so a note is never left half-walked.

The pure planning/selection logic lives in ``engine``; this module is the Anki glue tying
that to the threading + progress + media-write seams in ``core/anki_compat``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from omnia.core import anki_compat
from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH
from omnia.core.concurrency.pool import pooled_dispatch
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine import (
    GenerationResult,
    GenerationService,
    applies_to_deck,
    dedupe_preserving_order,
)
from omnia.plugins.smart_notes.engine.batching import SOLO_PLANNER, run_wave

if TYPE_CHECKING:
    from omnia.core.concurrency.dispatch import Dispatch
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
        SmartNotesSettings,
    )
    from omnia.plugins.smart_notes.engine.batching import FieldWork, WavePlanner
    from omnia.plugins.smart_notes.engine.note_run import NoteRun

logger = get_logger("smart_notes")

# Never publish progress to the Qt main thread more often than this. With many notes in flight
# rounds complete quickly, and an update per round would post hundreds of closures a second at
# the main thread for a bar the user cannot read that fast.
_PROGRESS_INTERVAL_SECONDS = 0.25

# The ceilings this build honours live on the settings model, next to the fields they bound
# (``SmartNotesSettings.workers`` / ``.notes_per_call``), so the batch runner, the editor
# button, review-time pre-generation and the GUI controller cannot disagree about them.


@dataclass
class _NotePlan:
    """One note's generation inputs, read on the main thread.

    The background op DOES touch the collection: media results are materialized as they are
    produced, so add_media_file runs inside the QueryOp. That is safe because Anki runs every
    QueryOp body on ONE thread (``TaskManager._collection_executor`` has a single worker) and
    because materialize is only ever called from that thread — never from a dispatch worker
    (see ``_run_cohort``). It is what lets a later tool read the reference the note will hold.
    These inputs are still read on the main thread."""

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
    # The note broke partway through — a gate, a commit or a media write raised. Whatever it
    # DID generate before that is still carried here and still written: the provider was
    # already paid for it, and a note whose fourth field could not be stored is not a reason to
    # throw away the three that could. The note is counted as ``failed`` either way, so the
    # user is told; the difference is whether the work survives.
    failed: bool = False


class _LiveNote:
    """One in-flight note: its plan, its :class:`NoteRun`, and its memoised materializer.

    Exists so the cohort loop can isolate a single note's failure. Anything a note's own gates
    or commit raise marks THAT note broken and lets the cohort carry on; without it, one bad
    note takes down every note sharing its wave, which the batch would then report — via
    ``on_failure`` — as the whole selection failing.
    """

    def __init__(
        self,
        plan: _NotePlan,
        run: NoteRun,
        materialize: Callable[[SmartNotesFieldRule, GenerationResult], str],
    ) -> None:
        self.plan = plan
        self.run = run
        self.materialize = materialize
        self.broken = False

    @property
    def resolved(self) -> bool:
        """Whether this note needs no further round (finished, or broken)."""
        return self.broken or self.run.done

    def next_works(self, service: GenerationService) -> list[FieldWork]:
        """This note's field work for the next dependency level (empty when it broke)."""
        try:
            return list(service.works_for(self.run))
        except Exception:
            logger.exception(
                "smart_notes: failed to plan the next fields of note %s", self.plan.nid
            )
            self.broken = True
            return []

    def commit(self, outcomes: list[Any]) -> None:
        """Apply the level's outcomes (chaining + media), on the caller's thread."""
        if self.broken:
            return  # its gates never ran this round, so there is nothing to apply
        try:
            self.run.commit(outcomes)
        except Exception:
            logger.exception("smart_notes: failed to generate note %s", self.plan.nid)
            self.broken = True

    def outcome(self) -> _NoteOutcome:
        """Turn the finished run into the outcome the main thread writes and counts.

        A BROKEN note still reports the results it had already committed. Discarding them threw
        away fields the provider had been paid for — and, for the media ones among them, left
        the bytes in the collection folder with no note referencing them — purely because a
        LATER field's write raised. It stays counted as ``failed``, which is the honest report;
        it just no longer loses the work.
        """
        results, blocked, failed = self.run.finish()
        if self.broken:
            return _NoteOutcome(
                self.plan.nid,
                materialize=self.materialize,
                results=results,
                failed=True,
            )
        for item in failed:
            logger.debug(
                "smart_notes: field %r produced nothing on note %s (%s): %s",
                item.field,
                self.plan.nid,
                item.kind,
                item.error,
            )
        return _NoteOutcome(
            self.plan.nid,
            materialize=self.materialize,
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


class _ProgressReporter:
    """Publishes "generating (n/total)" to the Qt main thread, monotonically and rarely.

    The counter is only ever advanced from the single driver thread, at commit, so it needs no
    lock and cannot go backwards — the "bar jumps around" failure mode of overlapping notes is
    designed out rather than patched. The closure reads the counter when the MAIN thread runs
    it (``ProgressManager.update`` keeps whatever value lands last), and publishes are
    coalesced so a fast round cannot flood the main thread with closures.
    """

    def __init__(self, total: int, *, enabled: bool = True) -> None:
        self._total = total
        self._enabled = enabled
        self._done = 0
        self._last_published = 0.0

    def advance(self, count: int) -> None:
        """Record ``count`` more finished notes and publish if it is time to."""
        if count <= 0:
            return
        self._done += count
        if not self._enabled:
            return
        now = time.monotonic()
        if (
            now - self._last_published < _PROGRESS_INTERVAL_SECONDS
            and self._done < self._total
        ):
            return
        self._last_published = now
        anki_compat.run_on_main(self._publish)

    def _publish(self) -> None:
        """Push the CURRENT counter to the dialog (runs on the Qt main thread)."""
        # Guarded inside the closure, not around the schedule: this runs later, on the main
        # thread, where an exception surfaces as a user-visible Anki error dialog for a
        # background job the user never asked about. A cosmetic count is not worth that.
        try:
            anki_compat.progress_update(
                f"Omnia: generating… ({self._done}/{self._total})",
                self._done,
                self._total,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("smart_notes: progress update failed")


def _cohorts(plans: list[_NotePlan], size: int) -> Iterator[list[_NotePlan]]:
    """Group ``plans`` into runs of at most ``size`` notes sharing ONE note-type config.

    Sharing a config is what makes a cohort worth overlapping: the notes then have the same
    dependency shape, so their levels line up and a round is not spent waiting for one note's
    fifth level while another has only one. It is also what makes K-note batching possible at
    all — every note in a round is then at the same dependency level, so the copies of one field
    that a chunk merges really are the same field with the same template.

    ``size`` is DERIVED, never a setting of its own: it is the wider of the worker count and the
    notes-per-call, because a cohort narrower than either would starve that mechanism, and one
    wider than both buys nothing while widening the window a cancel has to drain.
    """
    size = max(1, size)
    cohort: list[_NotePlan] = []
    note_type = ""
    for plan in plans:
        if cohort and (plan.config.note_type != note_type or len(cohort) >= size):
            yield cohort
            cohort = []
        note_type = plan.config.note_type
        cohort.append(plan)
    if cohort:
        yield cohort


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
        progress dialog, then generates off-thread (honouring cancel), and finally writes
        results + reports a summary on the main thread.

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
        workers = self._settings.workers()
        # The env knob's width, or 1 when it is -1 (batching off) — see
        # SmartNotesSettings.notes_per_call. It ships at 10, not off.
        notes_per_call = self._settings.notes_per_call()
        # One planner for the whole run: it carries the per-field output budget, which is only
        # worth learning if it survives from one chunk to the next.
        planner = self._service.batch_planner(notes_per_call=notes_per_call)
        if show_progress:
            anki_compat.progress_start(f"Omnia: generating… (0/{total})", total)

        def op() -> tuple[list[_NoteOutcome], bool]:
            # The pool is built HERE, inside the QueryOp body, and torn down before this
            # returns (pooled_dispatch's finally). Issuing more QueryOps instead would change
            # nothing: Anki runs them all on one single-worker executor.
            with pooled_dispatch(workers) as dispatch:
                return self._generate(
                    plans,
                    total,
                    force_overwrite=force_overwrite,
                    show_progress=show_progress,
                    dispatch=dispatch,
                    planner=planner,
                    cohort_size=max(workers, notes_per_call),
                )

        def on_success(result: tuple[list[_NoteOutcome], bool]) -> None:
            outcomes, cancelled = result
            try:
                summary = self._apply(outcomes)
                summary.skipped += deck_skipped
                summary.cancelled = cancelled
            finally:
                # In a `finally`: progress_start incremented Anki's GLOBAL progress refcount,
                # and a raise between here and progress_finish leaks it permanently — no
                # mw.progress.timer ever fires again and no dialog ever opens again, for the
                # rest of the session, in every add-on.
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
        show_progress: bool,
        dispatch: Dispatch,
        planner: WavePlanner,
        cohort_size: int,
    ) -> tuple[list[_NoteOutcome], bool]:
        """Generate every plan, cohort by cohort; returns the outcomes + the cancelled flag.

        **A cancel lands BETWEEN COHORTS, never inside one.** The flag is polled once per
        cohort, before that cohort starts; the cohort already running is walked to the end of
        its last dependency level. That is the pre-concurrency contract preserved exactly: a
        cancel could only ever land between whole notes, so no note is left with its first two
        levels written and its last three empty — a state the user cannot see, cannot fix
        except by regenerating, and which no summary bucket describes.

        The price is latency: a cancel now takes up to one cohort (at most
        ``max(workers, K)`` notes, generated concurrently) to take effect, against the five
        sequential notes it took before. Cohorts are what the notes are overlapped in, so this
        is the smallest boundary that is also a note boundary.

        Notes in the cohorts that are never started produce NO outcome at all: an outcome with
        no results reads downstream as "we tried and there was nothing to make", and that
        verdict DELETES clipped notes.
        """
        outcomes: list[_NoteOutcome] = []
        progress = _ProgressReporter(total, enabled=show_progress)
        for cohort in _cohorts(plans, cohort_size):
            # want_cancel() is a plain thread-safe flag on Anki's progress manager, so it can be
            # polled straight from this background thread. It is APP-wide, not run-scoped, so it
            # is read once per cohort and latched; nothing downstream re-reads the global.
            if show_progress and anki_compat.progress_was_cancelled():
                return outcomes, True
            outcomes.extend(
                self._run_cohort(
                    cohort,
                    force_overwrite=force_overwrite,
                    dispatch=dispatch,
                    planner=planner,
                    progress=progress,
                )
            )
        return outcomes, False

    def _run_cohort(
        self,
        plans: list[_NotePlan],
        *,
        force_overwrite: bool,
        dispatch: Dispatch = SEQUENTIAL_DISPATCH,
        planner: WavePlanner = SOLO_PLANNER,
        progress: Optional[_ProgressReporter] = None,
    ) -> list[_NoteOutcome]:
        """Walk one cohort's notes together, one dependency level per round, to the END.

        Every gate and every commit — and therefore every ``materialize``, hence every
        ``add_media_file`` — runs on THIS thread. Only the generation itself reaches
        ``dispatch``. That is what makes the per-note materializer memo safe without a lock.

        Runs to completion on purpose: a cohort is the unit a cancel is honoured between (see
        :meth:`_generate`), so this loop has no cancel poll and every note it returns has
        walked all of its levels.

        The keyword defaults describe a cohort of one with nothing to report to and nothing to
        batch — run it here, in this thread — which is what a single note is.
        """
        progress = progress or _ProgressReporter(len(plans), enabled=False)
        # Kept per PLAN, including the ones that could not even be planned, so the outcomes
        # come back in selection order however the cohort actually resolved.
        entries = [
            (plan, self._start(plan, force_overwrite=force_overwrite)) for plan in plans
        ]
        live = [note for _plan, note in entries if note is not None]
        progress.advance(len(entries) - len(live))

        counted = 0
        while any(not note.resolved for note in live):
            pending = [note for note in live if not note.resolved]
            works: list[FieldWork] = []
            spans: list[tuple[_LiveNote, int]] = []
            for note in pending:
                note_works = note.next_works(self._service)
                spans.append((note, len(note_works)))
                works.extend(note_works)
            outcome_slices = run_wave(planner.plan(works), len(works), dispatch)
            offset = 0
            for note, count in spans:
                note.commit(outcome_slices[offset : offset + count])
                offset += count
            finished = sum(1 for note in live if note.resolved)
            progress.advance(finished - counted)
            counted = finished
        return [
            _NoteOutcome(plan.nid, failed=True) if note is None else note.outcome()
            for plan, note in entries
        ]

    def _start(self, plan: _NotePlan, *, force_overwrite: bool) -> Optional[_LiveNote]:
        """Build ``plan``'s run, or return None when even planning it failed.

        Compiling the rules can raise (a cyclic config), and one bad note must not abort the
        cohort — the caller turns a None into this note's ``failed`` outcome.
        """
        try:
            # One materializer for the whole note, shared with _write_note below, so a
            # field's media is written once and the chain and the note agree on its name.
            materialize_once = note_materializer(plan.nid)
            run = self._service.make_run(
                plan.config,
                plan.fields,
                allow_empty_fields=self._settings.allow_empty_fields,
                force_overwrite=force_overwrite,
                materialize=materialize_once,
                note_id=plan.nid,
            )
        except Exception:  # one bad note must not abort the rest of the batch
            logger.exception("smart_notes: failed to generate note %s", plan.nid)
            return None
        return _LiveNote(plan, run, materialize_once)

    def _apply(self, outcomes: list[_NoteOutcome]) -> BatchSummary:
        """Write generated content back to the notes + media (main thread); count outcomes."""
        summary = BatchSummary()
        for outcome in outcomes:
            if outcome.failed:
                # Write whatever it managed before it broke (usually nothing), then count it as
                # failed. It must NOT reach ``empty_note_ids``: we do not know there was nothing
                # to make here, and that list's consumer DELETES the note.
                self._write_note(outcome)
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
