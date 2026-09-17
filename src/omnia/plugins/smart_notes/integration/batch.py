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

import logging
import re
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
from omnia.plugins.smart_notes.integration.progress import (
    ModalDialog,
    ProgressSurface,
    Silent,
)

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

# How many notes are written to the collection before the main thread is handed back to Qt.
#
# Small enough that a slice is well under a frame's worth of work even with media, large enough
# that a 1500-note batch is ~60 posted closures rather than 1500. The number that matters is not
# the throughput — the writes take what they take — but whether Anki repaints and answers input
# while they happen, and anything in this range does.
_WRITE_SLICE = 25

# How often to look again when the write-back is holding off for a reviewer.
#
# Polled rather than driven by Anki's state hook, because the question is "is it safe NOW" and a
# poll cannot miss an answer: a hook unsubscribed at the wrong moment, or a state change that
# happens while a slice is mid-flight, leaves the rest of a batch unwritten with nothing to
# restart it. Two seconds is under a card's reading time, so nothing waits long after the
# reviewer is closed.
_REVIEW_RECHECK_MS = 2000

#: Write-backs that have generated content in hand and have not written all of it yet.
#:
#: Held here rather than on the generator, because what has to find them is the profile closing —
#: a moment that knows nothing about which batch is running. Deferring the write while somebody
#: reviews is a courtesy; throwing away fields that cost real provider money because they quit
#: before finishing their reviews is not a trade anyone would choose, so closing flushes them.
_PENDING_WRITES: set[Callable[[], None]] = set()


def flush_pending_writes() -> None:
    """Write every batch's outstanding notes NOW. Called when the profile is closing."""
    for flush in list(_PENDING_WRITES):
        try:
            flush()
        except Exception:  # one batch must not strand another's content
            logger.exception("smart_notes: a pending write-back could not be flushed")
    _PENDING_WRITES.clear()


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


# How many named examples ("<field> needs <prereq>", "<field> — <chain trace>") the summary
# tooltip carries per category before it falls back to the count alone. The tooltip is read in
# passing, so this stays small on purpose.
_MAX_EXAMPLES = 2
# How much of one example's detail survives. A chain trace can run to a provider's full error
# body; past this the reader is scrolling a tooltip instead of reading it.
_MAX_EXAMPLE_CHARS = 90


def _example(field_name: str, detail: str) -> str:
    """Render one "<field> — <why>" example, clipped so a tooltip stays a tooltip."""
    text = " ".join(detail.split()) or "produced nothing"
    if len(text) > _MAX_EXAMPLE_CHARS:
        text = text[: _MAX_EXAMPLE_CHARS - 1].rstrip() + "…"
    return f"{field_name} — {text}"


def _merge_examples(into: list[str], examples: list[str]) -> None:
    """Add ``examples`` to ``into``, deduplicated and bounded by :data:`_MAX_EXAMPLES`.

    A batch is many notes of ONE note type, so the same field fails the same way on note after
    note; without the dedupe the two slots would both go to the first field and every other
    failing field would be invisible behind the count.
    """
    for example in examples:
        if example not in into and len(into) < _MAX_EXAMPLES:
            into.append(example)


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
    # A few "<field> — <chain trace>" strings for the two lists below. A bare count says a
    # field failed somewhere in a note type with twenty of them; the trace says which field it
    # was and which tool in its chain gave up, which is the whole of what the reader can act on.
    error_examples: list[str] = field(default_factory=list)
    unfilled_examples: list[str] = field(default_factory=list)
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
            parts.append(
                f"{self.blocked} blocked — missing prerequisites"
                f"{_suffix(self.blocked_examples)}"
            )
        if self.field_failures:
            parts.append(
                f"{self.field_failures} field error(s){_suffix(self.error_examples)}"
            )
        if self.unfilled:
            parts.append(
                f"{self.unfilled} field(s) had no applicable tool"
                f"{_suffix(self.unfilled_examples)}"
            )
        if self.tool_fallbacks:
            parts.append(f"{self.tool_fallbacks} field(s) fell back to a later tool")
        if self.errored_note_ids:
            # Says WHY a run that generated nothing still left notes behind — otherwise a user
            # with auto-discard on sees clips accumulating during an outage with no explanation.
            parts.append(f"kept {len(self.errored_note_ids)} note(s) for retry")
        prefix = "Cancelled — " if self.cancelled else ""
        return prefix + ", ".join(parts) + "."


def _examples(failed: list[Any], kind: str) -> list[str]:
    """The bounded "<field> — <why>" examples among ``failed`` of one kind.

    ``kind`` is matched as "error" vs everything else, mirroring ``FailedField.kind``: a chain
    that BROKE is a different report from one where every tool simply declined.
    """
    wanted = [item for item in failed if (item.kind == "error") == (kind == "error")]
    return [_example(item.field, item.error) for item in wanted[:_MAX_EXAMPLES]]


def _suffix(examples: list[str]) -> str:
    """Render the bounded "(<example>; <example>)" tail of a summary part, or ""."""
    detail = "; ".join(examples[:_MAX_EXAMPLES])
    return f" ({detail})" if detail else ""


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
    # "<field> — <chain trace>" for this note's failed/unproductive fields (bounded; see
    # ``BatchSummary``), so the summary can name one instead of only counting it.
    error_examples: list[str] = field(default_factory=list)
    unfilled_examples: list[str] = field(default_factory=list)
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
            # WARNING for a chain that BROKE, INFO for one that merely declined — and never
            # DEBUG, which is off in a normal profile. The summary tooltip names two of these;
            # the log is where the user goes for the rest, so it has to be there to find.
            logger.log(
                logging.WARNING if item.kind == "error" else logging.INFO,
                "smart_notes: field %r on note %s produced nothing (%s) — %s",
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
                for item in blocked[:_MAX_EXAMPLES]
            ],
            # Sliced AFTER the kind filter, not before: a note whose first two failures are
            # errors still has to be able to name an unproductive field further down the list.
            error_examples=_examples(failed, "error"),
            unfilled_examples=_examples(failed, "unproductive"),
            # A chain that ended empty-handed is only an ERROR when a tool actually broke;
            # "every tool declined" is its own, blameless outcome.
            field_failures=sum(1 for item in failed if item.kind == "error"),
            unfilled=sum(1 for item in failed if item.kind != "error"),
            tool_fallbacks=sum(
                1 for rule, result in results if _fell_back(rule, result)
            ),
        )


class _ProgressReporter:
    """Counts committed notes, monotonically and rarely, and hands the count to a surface.

    The counter is only ever advanced from the single driver thread, at commit, so it needs no
    lock and cannot go backwards — the "bar jumps around" failure mode of overlapping notes is
    designed out rather than patched.

    Publishes are coalesced. That is load-bearing for the modal surface, which has to reach the
    Qt main thread and would otherwise be handed hundreds of closures a second for a bar nobody
    can read that fast; it is merely harmless for the background one, which writes two integers
    under a lock. Keeping the throttle here rather than in each surface is what stops the two
    from disagreeing about how often "often" is.

    The LAST note always publishes, whatever the clock says — a batch that ends between ticks
    would otherwise leave its final frame one note short for as long as anyone is looking.
    """

    def __init__(self, total: int, surface: ProgressSurface) -> None:
        self._total = total
        self._surface = surface
        self._done = 0
        # None, NOT 0.0. `time.monotonic()` counts from an arbitrary origin — on Linux, boot —
        # so `now - 0.0` is only reliably "a long time" on a machine that has been up a while.
        # On a freshly started one it is a few seconds, and the FIRST update was throttled away:
        # a batch showed nothing at all until its last note. A sentinel says "never published"
        # without asking the clock what it means.
        self._last_published: Optional[float] = None

    def advance(self, count: int) -> None:
        """Record ``count`` more finished notes and publish if it is time to."""
        if count <= 0:
            return
        self._done += count
        now = time.monotonic()
        if (
            self._last_published is not None
            and now - self._last_published < _PROGRESS_INTERVAL_SECONDS
            and self._done < self._total
        ):
            return
        self._last_published = now
        self._surface.publish(self._done, self._total)


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
        surface: Optional[ProgressSurface] = None,
    ) -> None:
        """Generate smart fields for ``note_ids`` in the background, then call ``on_done``.

        Reads each note's fields + selects its enabled rules on the main thread, opens the
        progress dialog, then generates off-thread (honouring cancel), and finally writes
        results + reports a summary on the main thread.

        Args:
            note_ids: The notes to process (deduped here; cards of one note collapse to it).
            on_done: Main-thread callback receiving the :class:`BatchSummary`.
            surface: Where to report progress. Defaults to Anki's modal dialog. The choice
                decides whether Anki is usable while this runs: that dialog is
                ``ApplicationModal``, so the reviewer accepts no input until the batch is over.
                A :class:`~omnia.plugins.smart_notes.integration.progress.BackgroundBar` leaves
                the screen alone, which is what a user who started two hundred cards and went
                back to studying asked for. Background auto-generation passes ``Silent()``
                rather than a dialog, because a stream of clipped notes would otherwise stack
                modal windows — a summary tooltip still reports the result.
        """
        progress = surface if surface is not None else ModalDialog()
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
        progress.start(total)

        def op() -> tuple[list[_NoteOutcome], bool]:
            # The pool is built HERE, inside the QueryOp body, and torn down before this
            # returns (pooled_dispatch's finally). Issuing more QueryOps instead would change
            # nothing: Anki runs them all on one single-worker executor.
            with pooled_dispatch(workers) as dispatch:
                return self._generate(
                    plans,
                    total,
                    force_overwrite=force_overwrite,
                    progress=progress,
                    dispatch=dispatch,
                    planner=planner,
                    cohort_size=max(workers, notes_per_call),
                )

        def on_success(result: tuple[list[_NoteOutcome], bool]) -> None:
            outcomes, cancelled = result

            def finished(summary: BatchSummary) -> None:
                summary.skipped += deck_skipped
                summary.cancelled = cancelled
                # The modal surface's start() incremented Anki's GLOBAL progress refcount, and
                # never reaching its finish() leaks it permanently — no mw.progress.timer ever
                # fires again and no dialog ever opens again, for the rest of the session, in
                # every add-on. So this runs on every path out of the write, which is why
                # `_write_back` guarantees it is called exactly once.
                progress.finish()
                on_done(summary)

            self._write_back(outcomes, finished, progress)

        def on_failure(exc: Exception) -> None:
            progress.finish()
            logger.exception("smart_notes batch failed")
            on_done(BatchSummary(failed=total))

        # uses_collection=False is the whole reason a batch no longer freezes Anki behind a
        # modal "Processing…". Anki serialises every collection operation through ONE thread, so
        # holding it for the minutes this takes made the editor's own saves, and any other
        # collection work, queue behind the entire batch — and anything pending more than half a
        # second gets that window put over the app until it clears.
        #
        # The op qualifies because it is network and compute: the single piece of collection
        # access inside it is the media write in `materialize`, and `add_media_file` marshals
        # that to the main thread for the length of one file.
        anki_compat.run_in_background(
            op, on_success=on_success, on_failure=on_failure, uses_collection=False
        )

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
        progress: ProgressSurface,
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
        counter = _ProgressReporter(total, progress)
        for cohort in _cohorts(plans, cohort_size):
            # Asked once per cohort and latched. Both surfaces answer this from a plain
            # thread-safe flag, so it can be read straight from this background thread; the
            # dialog's is Anki's APP-wide want_cancel(), which is why nothing downstream
            # re-reads it.
            if progress.cancelled():
                return outcomes, True
            outcomes.extend(
                self._run_cohort(
                    cohort,
                    force_overwrite=force_overwrite,
                    dispatch=dispatch,
                    planner=planner,
                    progress=counter,
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
        progress = progress or _ProgressReporter(len(plans), Silent())
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

    def _write_back(
        self,
        outcomes: list[_NoteOutcome],
        done: Callable[[BatchSummary], None],
        progress: ProgressSurface,
    ) -> None:
        """Write every outcome back, in slices, handing the main thread back between each.

        Writing is main-thread work — ``col`` may not be touched from anywhere else — and it was
        done in ONE pass over every outcome. At the sizes this feature is for that is not a
        pause, it is a freeze: 1500 notes is 1500 backend transactions plus their media, and for
        the whole of it Anki paints nothing, answers no input, and cannot show or dismiss a
        window. A batch that ran in the background specifically so the user could keep studying
        then took the screen away at the very end, which is the one moment they had stopped
        watching for it.

        So the work is cut into slices and each is POSTED rather than called, through a QTimer.
        Qt returns to its event loop between one slice and the next, so Anki repaints and answers
        input throughout, and cards appear as they are written instead of all at once.

        ``done`` is called exactly once, on the main thread, whatever happens — the progress
        surface's refcount depends on it.
        """
        summary = BatchSummary()
        cursor = {"at": 0}
        finished = {"yes": False}

        def report(result: BatchSummary) -> None:
            """Hand the run back exactly once, however it got here."""
            if finished["yes"]:
                return
            finished["yes"] = True
            _PENDING_WRITES.discard(flush)
            done(result)

        def write_remaining() -> bool:
            """Write what is left, one slice. Returns whether there is still more."""
            chunk = outcomes[cursor["at"] : cursor["at"] + _WRITE_SLICE]
            cursor["at"] += len(chunk)
            self._apply(chunk, summary)
            return cursor["at"] < len(outcomes)

        def flush() -> None:
            """Write EVERYTHING now, regardless of the reviewer. For profile close.

            Waiting for the reviewer is a courtesy; losing generated content is not a courtesy
            anybody wants. By the time this runs the user is quitting or switching profiles, so
            there is no review left to protect — and what is in hand cost real money to make.
            """
            if finished["yes"]:
                return
            try:
                while write_remaining():
                    pass
            except Exception:
                logger.exception("smart_notes: flushing the batch on close failed")
            report(summary)

        def step() -> None:
            if finished["yes"]:
                return  # a close flushed it while this tick was in the queue
            if anki_compat.reviewing():
                # HOLD. Any note write makes the reviewer redraw the card on screen — it keys
                # on "some note changed", not on which one — and the redraw rebuilds the
                # webview, throwing away a half-typed answer. Someone who started a long batch
                # so they could carry on studying would be interrupted by the very thing they
                # started. The media is already on disk; only the field updates wait, and they
                # go in the moment the reviewer is left.
                progress.hold("waiting until you finish reviewing")
                anki_compat.single_shot(_REVIEW_RECHECK_MS, step)
                return
            progress.hold("")
            try:
                more = write_remaining()
            except Exception:
                # One bad slice must not strand the run: the counts are already in `summary`,
                # and abandoning here would leak the progress refcount with it.
                logger.exception("smart_notes: writing a slice of the batch failed")
                report(summary)
                return
            if more:
                # single_shot, NOT run_on_main. `TaskManager.run_on_main` appends to a list and
                # emits a pyqtSignal — and a signal emitted from the thread it is connected on
                # is delivered DIRECTLY, i.e. synchronously. Called from the main thread it
                # therefore runs the next slice inside this one, so the event loop is never
                # reached (Anki stays frozen for the whole write-back, which this was written
                # to prevent) and the slices nest: 8444 notes is 338 of them, and Python ran
                # out of stack before it ran out of notes. A QTimer genuinely posts.
                anki_compat.single_shot(0, step)
                return
            report(summary)

        _PENDING_WRITES.add(flush)
        step()

    def _apply(
        self, outcomes: list[_NoteOutcome], summary: Optional[BatchSummary] = None
    ) -> BatchSummary:
        """Write generated content back to the notes + media (main thread); count outcomes.

        Counts INTO ``summary`` when one is given, because the caller writes in slices and one
        run's totals span all of them. Without one it answers for just what it was handed, which
        is what a caller writing everything at once wants.
        """
        summary = BatchSummary() if summary is None else summary
        # Filled as the outcomes are walked and persisted ONCE at the end, because one
        # `col.update_note` per note is one backend transaction, one undo entry and one
        # `operation_did_execute` per note. At 1500 notes that is 1500 of each — the Browser,
        # the sidebar and every other add-on listening re-run 1500 times, and the user's undo
        # history becomes 1500 steps deep. `update_notes` is the same write as one batch.
        pending: list[Any] = []
        # Files the fields being rewritten will stop referencing. Trashed only after the write
        # lands, never before — a write that failed would otherwise have deleted the audio the
        # note still points at.
        superseded: list[str] = []
        for outcome in outcomes:
            if outcome.failed:
                # Write whatever it managed before it broke (usually nothing), then count it as
                # failed. It must NOT reach ``empty_note_ids``: we do not know there was nothing
                # to make here, and that list's consumer DELETES the note.
                prepared = self._prepare_note(outcome, superseded)
                if prepared is not None:
                    pending.append(prepared)
                summary.failed += 1
                continue
            summary.blocked += outcome.blocked
            _merge_examples(summary.blocked_examples, outcome.blocked_examples)
            _merge_examples(summary.error_examples, outcome.error_examples)
            _merge_examples(summary.unfilled_examples, outcome.unfilled_examples)
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
            prepared = self._prepare_note(outcome, superseded)
            if prepared is not None:
                pending.append(prepared)
                summary.processed += 1
            else:
                summary.failed += 1
        self._persist(pending, summary, superseded)
        return summary

    def _persist(
        self,
        notes: list[Any],
        summary: BatchSummary,
        superseded: Optional[list[str]] = None,
    ) -> None:
        """Write the prepared notes as ONE batch, and count them as failed if that will not.

        The counts were already moved to ``processed`` while preparing, because that is where
        "did this note get content" is known; a batch that then cannot be written moves them
        back rather than reporting a save that did not happen.

        ``superseded`` is trashed only once the write has SUCCEEDED, for the same reason: media
        the note still references must survive a failed write.
        """
        if not notes:
            return
        try:
            anki_compat.update_notes(notes)
        except Exception:
            logger.exception("smart_notes: writing %d notes failed", len(notes))
            summary.processed -= len(notes)
            summary.failed += len(notes)
            return
        if superseded:
            try:
                anki_compat.trash_media_files(superseded)
            except Exception:  # disk space is not worth failing a written batch over
                logger.exception(
                    "smart_notes: could not trash %d superseded media file(s)",
                    len(superseded),
                )

    def _prepare_note(
        self, outcome: _NoteOutcome, superseded: Optional[list[str]] = None
    ) -> Optional[Any]:
        """The note with this outcome's content in it, unsaved, or ``None``.

        Separate from writing so a slice can be persisted as one batch. ``None`` means there is
        nothing to save — either the note could not be read, or no rule targeted a field it has.

        Args:
            outcome: What was generated for this note.
            superseded: Appended with the media files the fields STOP referencing, for the
                caller to trash once the write has landed. Not trashed here: a write that then
                fails would have deleted the audio the note still points at.
        """
        superseded = [] if superseded is None else superseded
        try:
            note = anki_compat.get_note(outcome.nid)
            wrote = False
            for rule, result in outcome.results:
                if rule.target_field not in note:
                    continue
                # What the field pointed at BEFORE this run, so the file it is about to stop
                # referencing can be trashed once the new value is safely stored. Collected
                # here because this is the only moment both values are in hand.
                superseded.extend(superseded_media(note[rule.target_field]))
                note[rule.target_field] = outcome.materialize(rule, result)
                wrote = True
            return note if wrote else None
        except Exception:
            logger.exception("smart_notes: failed to fill note %s", outcome.nid)
            return None


def _fell_back(rule: SmartNotesFieldRule, result: GenerationResult) -> bool:
    """Whether a NON-FIRST tool of ``rule``'s chain produced ``result``.

    Reads the provenance the pipeline stamps on the result (``GenerationResult.tool``) against
    the chain's first entry. An unstamped result (a rule generated outside the pipeline in a
    test) counts as no fallback.
    """
    first = rule.tools[0].name if rule.tools else ""
    return bool(result.tool) and result.tool != first


#: The prefix :func:`materialize` gives every file it writes.
#:
#: Load-bearing, because it is the ONLY thing that decides whether a file may be trashed when a
#: field is regenerated. A real collection holds media from several sources — AwesomeTTS and
#: HyperTTS write ``googletts-…``, pasted images land as ``paste-…``, other add-ons have their
#: own families — and none of them are ours to delete. A name we did not write is left alone,
#: whatever it looks like.
OUR_MEDIA_PREFIX = "omnia-"

#: A media reference inside a field: ``[sound:x.mp3]`` or ``<img src="x.png">``. Both quote
#: styles, because Anki's editor writes double and hand-edited fields carry single.
_MEDIA_REF_RE = re.compile(
    r"\[sound:([^\]]+)\]|<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE
)


def superseded_media(previous: str) -> list[str]:
    """The files ``previous`` referenced that Omnia wrote, and may therefore replace.

    Regenerating a field does NOT overwrite the old file: ``MediaManager.write_data`` renames on
    collision ("renaming if not unique"), so every regeneration leaves the previous audio behind
    referenced by nobody. Five regenerations of one field is five files and one useful one. On a
    real collection that ran to hundreds of megabytes of audio nothing could play.

    Only files carrying :data:`OUR_MEDIA_PREFIX` are returned. Everything else in the field —
    another add-on's TTS, a pasted image — is somebody else's and is not ours to remove.
    """
    out: list[str] = []
    for sound, image in _MEDIA_REF_RE.findall(previous or ""):
        name = (sound or image).strip()
        if name.startswith(OUR_MEDIA_PREFIX) and name not in out:
            out.append(name)
    return out


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
