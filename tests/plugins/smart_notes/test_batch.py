"""Tests for the smart_notes batch generator (the cancellable, counted batch glue).

The batch runner is Anki glue, but its orchestration — dedupe, per-note rule selection,
chunked generation, cancel handling, and the success/fail/skip summary — is exercised here by
faking the ``anki_compat`` seams it calls (collection reads, progress dialog, media writes).
The ``run_in_background`` seam is the synchronous QueryOp stub from ``conftest``, so the whole
flow runs inline.
"""

from __future__ import annotations

import contextlib

import pytest
from conftest import FakeLLMProvider

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration.batch import (
    BatchGenerator,
    BatchSummary,
    _PreparedNote,
)


def _text(note, field):
    """A field as a READER sees it — without Omnia's provenance mark.

    Omnia stamps the text it writes with `<!--omnia:hash-->` so a later regeneration can tell
    its own output from something the user typed. The mark is invisible while reviewing, so a
    test asserting what the note now says should not see it either.
    """
    from omnia.plugins.smart_notes.provenance import unstamp

    return unstamp(note[field])


def _note_type_config(note_type="Basic", *, enabled=True, decks=None):
    """A Basic note type whose 'Def' field is generated from the 'Word' base field."""
    return SmartNotesNoteTypeConfig(
        note_type=note_type,
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Def", enabled=enabled, type="text", prompt="define {{Word}}"
            )
        ],
        decks=list(decks or []),
    )


class _FakeCard:
    def __init__(self, did: int) -> None:
        self.did = did


class _FakeNote:
    """A dict-like note exposing ``keys()`` + ``note_type()`` + ``cards()`` like Anki's Note."""

    def __init__(
        self, nid: int, note_type: str, fields: dict[str, str], decks=(1,)
    ) -> None:
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)
        self._decks = list(decks)

    def keys(self):
        return list(self._fields.keys())

    def __contains__(self, key: str) -> bool:
        return key in self._fields

    def __getitem__(self, key: str) -> str:
        return self._fields[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._fields[key] = value

    def note_type(self) -> dict[str, str]:
        return {"name": self._note_type}

    def cards(self):
        return [_FakeCard(did) for did in self._decks]


class _StubHub:
    def __init__(self, llm) -> None:
        self._llm = llm

    def llm(self, *, model: str = "", provider: str = ""):
        return self._llm

    def tts(self):
        raise AssertionError("no TTS in these tests")

    def tts_speed(self):
        """The central pace every field falls back to; 1.0 is the voice's own."""
        return 1.0


class _FakeCompat:
    """Records progress calls + note writes; drives cancel via a queued flag list."""

    def __init__(self, notes: dict[int, _FakeNote], cancel_after: int | None = None):
        self._notes = notes
        self.updated: list[int] = []
        self.progress: list[str] = []
        self.media: list[str] = []
        self._cancel_after = cancel_after
        self._cancel_polls = 0
        self.run_on_main_calls = 0
        self.in_review = False
        self.pending_timers: list = []
        self.trashed: list = []
        #: Filenames some OTHER note still points at — a note duplicated in the Browser keeps
        #: the same ``[sound:…]``, so these must survive a regeneration of the original.
        self.still_referenced: set = set()

    # collection
    def get_note(self, nid, col=None):
        return self._notes[nid]

    def note_deck_ids(self, note, col=None):
        return [int(c.did) for c in note.cards()]

    def update_notes(self, notes, col=None):
        """One write for many notes — what the batch actually uses.

        Recorded into the same `updated` list as the singular form, because what every test
        here cares about is WHICH notes were persisted, not how many transactions it took.
        """
        for note in notes:
            self.update_note(note)

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def trash_media_files(self, filenames, col=None):
        self.trashed.extend(filenames)

    def media_still_referenced(self, filenames, col=None):
        return {name for name in filenames if name in self.still_referenced}

    def add_media_file(self, filename, data, col=None):
        self.media.append(filename)
        return filename

    # progress
    def progress_start(self, label, maximum):
        self.progress.append(label)

    def progress_update(self, label, value, maximum):
        self.progress.append(label)

    def progress_finish(self):
        self.progress.append("finish")

    def progress_was_cancelled(self):
        self._cancel_polls += 1
        return (
            self._cancel_after is not None and self._cancel_polls > self._cancel_after
        )

    # reviewer / timers
    def reviewing(self):
        return getattr(self, "in_review", False)

    def single_shot(self, milliseconds, callback):
        """Hold the callback instead of firing it, so a test drives the clock itself."""
        self.pending_timers.append(callback)

    def fire_timers(self):
        """Run whatever is waiting, once — the tick Anki's timer would have delivered."""
        waiting, self.pending_timers = self.pending_timers, []
        for callback in waiting:
            callback()

    def drain_timers(self, limit=5000):
        """Keep delivering ticks until nothing is waiting.

        Counts them, because the COUNT is the evidence a slice was deferred rather than called
        inline: a write-back that recurses finishes with zero ticks and an exhausted stack.
        """
        ticks = 0
        while self.pending_timers and ticks < limit:
            self.fire_timers()
            ticks += 1
        return ticks

    # threading
    def run_on_main(self, callback):
        self.run_on_main_calls += 1
        callback()

    def run_in_background(
        self, op, *, on_success, on_failure=None, label=None, uses_collection=True
    ):
        # `uses_collection` is recorded rather than ignored: a batch that stopped asking
        # for the collection thread is the difference between Anki staying usable and
        # Anki putting a modal window over itself for the length of the run.
        self.uses_collection = uses_collection
        try:
            on_success(op())
        except Exception as exc:  # mirror QueryOp routing
            if on_failure:
                on_failure(exc)


@pytest.fixture(autouse=True)
def _no_write_backs_leak_between_tests():
    """Clear the module-level set of unfinished write-backs around every test.

    It is global on purpose — what has to find a waiting write-back is the profile closing,
    which knows nothing about which batch is running. The cost is that a test leaving one
    deferred (an open reviewer that never closes) hands it to the next test, where
    `flush_pending_writes()` runs it against whatever fake is patched in by then and writes its
    notes a second time. That is a test artefact, not a product one, but it fails loudly and
    confusingly, so it is cut here.
    """
    from omnia.plugins.smart_notes.integration.batch import _PENDING_WRITES

    _PENDING_WRITES.clear()
    yield
    _PENDING_WRITES.clear()


def _patch_compat(monkeypatch, fake: _FakeCompat) -> None:
    import omnia.plugins.smart_notes.integration.batch as batch

    for name in (
        "get_note",
        "note_deck_ids",
        "update_note",
        "update_notes",
        "add_media_file",
        "trash_media_files",
        "media_still_referenced",
        "progress_start",
        "progress_update",
        "progress_finish",
        "progress_was_cancelled",
        "run_on_main",
        "run_in_background",
        "reviewing",
        "single_shot",
    ):
        monkeypatch.setattr(batch.anki_compat, name, getattr(fake, name))


def _generator(settings: SmartNotesSettings) -> GenerationService:
    return GenerationService(_StubHub(FakeLLMProvider(text="generated")))


class _CannedRun:
    """A :class:`NoteRun` stand-in that finishes in one empty round with a fixed triple."""

    def __init__(self, triple) -> None:
        self._triple = triple
        self.done = False

    def commit(self, outcomes) -> None:
        assert outcomes == []

    def finish(self):
        return self._triple


class _CannedService:
    """A GenerationService stand-in: every note resolves at once to one canned triple.

    Lets a test drive the REAL cohort runner (gates, commit, outcome building, `_apply`)
    against an exact ``(results, blocked, failed)`` without standing up providers.
    """

    def __init__(self, results=(), blocked=(), failed=()) -> None:
        self._triple = (list(results), list(blocked), list(failed))
        self.materializers: list = []

    def make_run(self, config, fields, **kwargs):
        self.materializers.append(kwargs.get("materialize"))
        return _CannedRun(self._triple)

    def works_for(self, run):
        run.done = True
        return []


class TestBatchGenerator:
    def _settings(self, **kw) -> SmartNotesSettings:
        base = {
            "note_types": [_note_type_config()],
            "regenerate_when_batching": False,
        }
        base.update(kw)
        return SmartNotesSettings(**base)

    def test_generates_and_writes_each_note(self, monkeypatch):
        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}),
            2: _FakeNote(2, "Basic", {"Word": "dog", "Def": ""}),
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1, 2], summaries.append)
        assert fake.updated == [1, 2]
        assert summaries[0].processed == 2
        assert _text(notes[1], "Def") == "generated"

    def test_dedupes_note_ids(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1, 1, 1], summaries.append)
        assert summaries[0].processed == 1
        assert fake.updated == [1]

    def test_already_filled_target_is_skipped(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "filled"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(regenerate_when_batching=False)
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].skipped == 1
        assert fake.updated == []

    def test_regenerate_when_batching_overwrites_filled_target(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "old"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(regenerate_when_batching=True)
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].processed == 1
        assert _text(notes[1], "Def") == "generated"

    def test_notes_without_matching_rules_are_dropped(self, monkeypatch):
        notes = {1: _FakeNote(1, "Cloze", {"Text": "x"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0] == summaries[0]  # ran
        assert summaries[0].processed == 0
        assert fake.progress == []  # never opened progress for an empty plan

    def test_cancel_stops_between_cohorts_and_emits_no_outcome_for_undispatched_notes(
        self, monkeypatch
    ):
        notes = {
            n: _FakeNote(n, "Basic", {"Word": "w", "Def": ""}) for n in range(1, 13)
        }
        # Cancel is polled once per COHORT, before it starts; let the first through.
        fake = _FakeCompat(notes, cancel_after=1)
        _patch_compat(monkeypatch, fake)
        # K = 1 so the cohort is the WORKER count and nothing else: a cohort is
        # ``max(workers, K)``, and this test is about cancel granularity, not about batching.
        settings = self._settings(max_concurrent_generations=3, batch_notes_per_call=1)
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(range(1, 13)), summaries.append
        )
        assert summaries[0].cancelled is True
        # Exactly the first cohort (3 = max_concurrent_generations) was generated + written.
        assert fake.updated == [1, 2, 3]
        assert summaries[0].processed == 3
        # The notes never dispatched must leave NO trace: an outcome with no results reads as
        # "we tried and there was nothing to make", and that verdict deletes clipped notes.
        assert summaries[0].empty_note_ids == []
        assert summaries[0].errored_note_ids == []
        assert summaries[0].skipped == 0

    def test_a_cancel_never_leaves_a_note_half_generated(self, monkeypatch):
        """Every note the run touched is COMPLETE, and every one of them is counted.

        The regression this pins: polling cancel once per dependency LEVEL cut notes mid-walk,
        so a note came out with its first level written and its later levels empty — written to
        the collection, and counted in no summary bucket, so the tooltip said "Processed 3" while
        five notes had been modified. Two levels here (``Def`` from ``Word``, ``Extra`` from
        ``Def``) are what make a half-walk visible at all; a one-level note type cannot show it.
        """
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Def", enabled=True, type="text", prompt="define {{Word}}"
                ),
                SmartNotesFieldConfig(
                    field="Extra", enabled=True, type="text", prompt="expand {{Def}}"
                ),
            ],
        )
        notes = {
            n: _FakeNote(n, "Basic", {"Word": f"w{n}", "Def": "", "Extra": ""})
            for n in range(1, 13)
        }
        # Cancel from the very first poll of the SECOND cohort onwards; with two levels per
        # note, a per-level poll would have fired inside the first cohort's second round.
        fake = _FakeCompat(notes, cancel_after=1)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[config],
            regenerate_when_batching=False,
            max_concurrent_generations=3,
            # As above: K = 1 pins the cohort to the worker count.
            batch_notes_per_call=1,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(range(1, 13)), summaries.append
        )

        assert summaries[0].cancelled is True
        touched = sorted(set(fake.updated))
        assert touched == [1, 2, 3]
        # Complete, not half-walked: BOTH levels are filled on every note that was written.
        for nid in touched:
            assert _text(notes[nid], "Def"), f"note {nid} lost its first level"
            assert _text(notes[nid], "Extra"), f"note {nid} lost its second level"
        # And every touched note is accounted for — none silently in no bucket.
        assert summaries[0].processed == len(touched)


class TestWritingBackDoesNotHogTheMainThread:
    """Writing is main-thread work, and it used to be done in ONE pass over every outcome.

    At the sizes this feature exists for that is not a pause, it is a freeze: 1500 notes is 1500
    backend transactions plus their media, and for the whole of it Anki paints nothing and
    answers no input. A batch that ran in the background so the user could keep studying then
    took the screen away at the very end — the one moment they had stopped watching for it.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _many(self, monkeypatch, count: int, slice_size: int):
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module, "_WRITE_SLICE", slice_size)
        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, count + 1)
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        return fake, list(notes)

    def test_each_slice_waits_for_the_event_loop(self, monkeypatch):
        """The slices must be POSTED, not called — and the difference is not academic.

        `TaskManager.run_on_main` appends to a list and emits a pyqtSignal, and a signal emitted
        on the thread it is connected to is delivered DIRECTLY. Called from the main thread it
        therefore ran the next slice inside the current one: the event loop was never reached,
        so Anki stayed frozen for the whole write-back, and the slices nested until Python ran
        out of stack. 8444 notes is 338 slices and a RecursionError.

        Counting calls could not tell those apart — a recursive hand-off calls just as often.
        What distinguishes them is that the work is still WAITING when `run` returns.
        """
        from omnia.plugins.smart_notes.integration.progress import Silent

        fake, nids = self._many(monkeypatch, 60, 10)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=Silent()
        )

        assert len(fake.updated) == 10, "it wrote past the first slice without yielding"
        assert fake.pending_timers, "the next slice was never posted"

        ticks = fake.drain_timers()

        assert fake.updated == nids
        assert (
            ticks == 5
        ), f"60 notes in tens is five ticks after the first slice, got {ticks}"

    def test_every_note_is_still_written(self, monkeypatch):
        # Slicing must not drop the tail, which is the obvious way to get this wrong.
        fake, nids = self._many(monkeypatch, 57, 10)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run(nids, lambda _s: None)
        fake.drain_timers()

        assert fake.updated == nids

    def test_the_counts_span_every_slice(self, monkeypatch):
        # One summary for the run, not one per slice.
        _fake, nids = self._many(monkeypatch, 57, 10)
        settings = self._settings()
        summaries: list = []

        BatchGenerator(_generator(settings), settings).run(nids, summaries.append)
        _fake.drain_timers()

        assert (
            len(summaries) == 1
        ), "the caller was told the batch finished more than once"
        assert summaries[0].processed == 57

    def test_a_slice_is_one_write_not_one_per_note(self, monkeypatch):
        """One `col.update_note` per note is one backend transaction, one undo entry and one
        `operation_did_execute` PER NOTE.

        At 1500 notes that is 1500 of each: the Browser, the sidebar and every other add-on
        listening re-run 1500 times, and the user's undo history becomes 1500 steps deep. The
        same content written as a batch is one of each per slice.
        """
        from omnia.plugins.smart_notes.integration.progress import Silent

        _fake, nids = self._many(monkeypatch, 40, 10)
        settings = self._settings()
        batches: list[int] = []
        import omnia.plugins.smart_notes.integration.batch as batch_module

        monkeypatch.setattr(
            batch_module.anki_compat,
            "update_notes",
            lambda notes, col=None: batches.append(len(notes)),
        )

        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=Silent()
        )
        _fake.drain_timers()

        assert batches == [10, 10, 10, 10], batches

    def test_a_batch_that_cannot_be_written_is_counted_as_failed(self, monkeypatch):
        # Not as processed. Reporting a save that did not happen is how a user goes looking for
        # cards that are not there.
        from omnia.plugins.smart_notes.integration.progress import Silent

        _fake, nids = self._many(monkeypatch, 20, 10)
        settings = self._settings()
        import omnia.plugins.smart_notes.integration.batch as batch_module

        def refuse(notes, col=None):
            raise RuntimeError("collection is locked")

        monkeypatch.setattr(batch_module.anki_compat, "update_notes", refuse)
        summaries: list = []

        BatchGenerator(_generator(settings), settings).run(
            nids, summaries.append, surface=Silent()
        )
        _fake.drain_timers()

        assert summaries[0].processed == 0
        assert summaries[0].failed == 20

    def test_the_progress_surface_is_finished_exactly_once(self, monkeypatch):
        # It is a refcount on Anki's GLOBAL progress manager: finishing twice, or never,
        # breaks every add-on's dialogs for the rest of the session.
        from omnia.plugins.smart_notes.integration.progress import ProgressSurface

        class Counter(ProgressSurface):
            finishes = 0

            def finish(self):
                Counter.finishes += 1

        _fake, nids = self._many(monkeypatch, 40, 10)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=Counter()
        )
        _fake.drain_timers()

        assert Counter.finishes == 1

    def test_a_slice_that_explodes_still_ends_the_run(self, monkeypatch):
        # Abandoning mid-chain would strand the caller AND leak the progress refcount.
        _fake, nids = self._many(monkeypatch, 40, 10)
        settings = self._settings()
        gen = BatchGenerator(_generator(settings), settings)
        calls = {"n": 0}
        real = gen._apply

        def boom(outcomes, summary=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk on fire")
            return real(outcomes, summary)

        monkeypatch.setattr(gen, "_apply", boom)
        summaries: list = []

        gen.run(nids, summaries.append)
        _fake.drain_timers()

        assert len(summaries) == 1, "the run never reported back"


class TestTheBatchDoesNotHoldAnkisCollectionThread:
    """Why a long batch used to put a modal "Processing…" window over the whole app.

    `TaskManager` owns exactly one collection thread (`ThreadPoolExecutor(max_workers=1)`), so
    every collection operation in Anki is serialised through it. A batch that held it for the
    minutes a real run takes made everything else queue behind the WHOLE batch — and
    `ProgressManager._maybeShow` puts up its unlabelled "Processing…" for anything pending more
    than half a second. So the window was not a symptom of Anki being busy; it was the editor's
    own save waiting its turn behind us, and it stayed until the batch ended.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def test_the_op_body_touches_the_collection_only_through_media(self, monkeypatch):
        """The invariant that makes releasing the thread safe, pinned.

        Anki's rule is one thread on the collection at a time, and the single collection thread
        is how it enforces that. Letting go of it is only sound while the op's own collection
        access is marshalled — today that is exactly one call, the media write. A `get_note` or
        an `update_note` added to the generation path later would break the rule silently, so
        this runs the op on a REAL second thread and fails if anything but media is reached
        from it.
        """
        import threading

        main = threading.current_thread().name
        offences: list = []

        class _Strict(_FakeCompat):
            def _check(self, what):
                if threading.current_thread().name != main:
                    offences.append(what)

            def get_note(self, nid, col=None):
                self._check("get_note")
                return super().get_note(nid, col)

            def update_notes(self, notes, col=None):
                self._check("update_notes")
                return super().update_notes(notes, col)

            def note_deck_ids(self, note, col=None):
                self._check("note_deck_ids")
                return super().note_deck_ids(note, col)

            def run_in_background(
                self,
                op,
                *,
                on_success,
                on_failure=None,
                label=None,
                uses_collection=True,
            ):
                # A real second thread, like Anki's. Running the op inline would put it on the
                # main thread and this test could never fail.
                self.uses_collection = uses_collection
                box: dict = {}

                def runner():
                    try:
                        box["value"] = op()
                    except Exception as exc:
                        box["error"] = exc

                thread = threading.Thread(target=runner, name="op-thread")
                thread.start()
                thread.join(10)
                if "error" in box:
                    if on_failure:
                        on_failure(box["error"])
                    return
                on_success(box["value"])

        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, 4)
        }
        _patch_compat(monkeypatch, _Strict(notes))
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run([1, 2, 3], lambda _s: None)

        assert offences == [], (
            "the generation op reached the collection off the main thread: "
            + ", ".join(sorted(set(offences)))
        )

    def test_the_generation_op_releases_it(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run([1], lambda _s: None)

        assert fake.uses_collection is False, (
            "the batch asked for Anki's single collection thread and will hold it for the "
            "whole run, so every other collection operation queues behind it"
        )


class TestWritingWaitsForTheReviewer:
    """Generating in the background is pointless if it interrupts the studying it was for.

    Any note write at all makes the reviewer redraw the card on screen: `Reviewer.op_executed`
    keys on `changes.note_text` with no check of WHICH note changed, and `_redraw_current_card`
    re-runs `_showQuestion`, which rebuilds the webview — including the type-in box. So a batch
    writing 1500 notes while someone studies flickers their card about sixty times and throws
    away any answer they were part-way through typing.

    The media is on disk either way; only the field updates wait.

    Every test here runs on a NON-blocking surface, because that is the only surface the wait
    makes sense on — see ``TestAModalRunNeverWaitsForTheReviewer``.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _surface(self):
        from omnia.plugins.smart_notes.integration.progress import Silent

        return Silent()

    def _running(self, monkeypatch, count=4, reviewing=True):
        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, count + 1)
        }
        fake = _FakeCompat(notes)
        fake.in_review = reviewing
        _patch_compat(monkeypatch, fake)
        return fake, list(notes)

    def test_nothing_is_written_while_a_card_is_on_screen(self, monkeypatch):
        fake, nids = self._running(monkeypatch)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=self._surface()
        )

        assert fake.updated == [], "it rewrote notes under someone who was studying"
        assert fake.pending_timers, "it gave up instead of waiting"

    def test_it_writes_as_soon_as_the_reviewer_is_left(self, monkeypatch):
        fake, nids = self._running(monkeypatch)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            nids, summaries.append, surface=self._surface()
        )
        assert fake.updated == []

        fake.in_review = False
        fake.fire_timers()

        assert fake.updated == nids
        assert len(summaries) == 1, "the run never reported back"

    def test_it_keeps_waiting_for_as_long_as_the_reviewer_is_open(self, monkeypatch):
        # Re-arms rather than giving up after one look: a review session is many minutes.
        fake, nids = self._running(monkeypatch)
        settings = self._settings()
        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=self._surface()
        )

        for _ in range(5):
            fake.fire_timers()

        assert fake.updated == []
        assert fake.pending_timers, "it stopped watching and the batch would never land"

    def test_a_batch_started_outside_review_writes_straight_away(self, monkeypatch):
        # The ordinary case must not pay a two-second wait for a reviewer that is not there.
        fake, nids = self._running(monkeypatch, reviewing=False)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run(
            nids, lambda _s: None, surface=self._surface()
        )

        assert fake.updated == nids
        assert fake.pending_timers == []

    def test_review_started_mid_write_stops_the_remaining_slices(self, monkeypatch):
        """The check is per slice, not once at the start.

        Someone can open the reviewer while a batch is already writing, and the slices still to
        come must notice — otherwise the first card they see is redrawn under them.
        """
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module, "_WRITE_SLICE", 2)
        fake, nids = self._running(monkeypatch, count=6, reviewing=False)
        settings = self._settings()
        gen = BatchGenerator(_generator(settings), settings)
        real = gen._apply

        def start_reviewing(outcomes, summary=None):
            result = real(outcomes, summary)
            fake.in_review = True  # they open the reviewer after the first slice lands
            return result

        monkeypatch.setattr(gen, "_apply", start_reviewing)

        gen.run(nids, lambda _s: None, surface=self._surface())

        assert fake.updated == nids[:2], fake.updated
        assert fake.pending_timers, "the rest of the batch wrote over an open reviewer"


class TestAFailedWriteRollsBackOnlyWhatItCounted:
    """A slice's notes were not all counted the same way, so it cannot roll back as a block.

    A note whose generation FAILED is still written — it keeps whatever the run managed before
    it broke — but it is counted under ``failed``, never ``processed``. Subtracting the whole
    slice from ``processed`` therefore removes notes that were never added there, and a mixed
    slice that fails to write reports a NEGATIVE processed count straight into the summary the
    user reads, plus one failure per note more than there were notes.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _apply(self, outcomes, *, write_fails: bool):
        """Count these outcomes, with the batched write either landing or raising."""
        gen = BatchGenerator.__new__(BatchGenerator)
        gen._prepare_note = (  # type: ignore[method-assign]
            lambda outcome, *, counted_as_processed: _PreparedNote(
                _FakeNote(outcome.nid, "Basic", {}), counted_as_processed
            )
        )
        import omnia.plugins.smart_notes.integration.batch as batch_module

        calls: list = []

        def update_notes(notes, col=None):
            calls.append(notes)
            if write_fails:
                raise RuntimeError("collection is locked")

        original = batch_module.anki_compat.update_notes
        batch_module.anki_compat.update_notes = update_notes
        try:
            return gen._apply(outcomes)
        finally:
            batch_module.anki_compat.update_notes = original

    def _outcomes(self):
        from omnia.plugins.smart_notes.integration.batch import _NoteOutcome

        return [
            # Broke mid-note, but produced something on the way — written, counted `failed`.
            _NoteOutcome(1, failed=True, results=[("rule", "result")]),
            _NoteOutcome(2, results=[("rule", "result")]),  # counted `processed`
        ]

    def test_a_successful_write_counts_each_note_once(self):
        summary = self._apply(self._outcomes(), write_fails=False)

        assert (summary.processed, summary.failed) == (1, 1)

    def test_a_failed_write_does_not_invent_counts(self):
        summary = self._apply(self._outcomes(), write_fails=True)

        assert (
            summary.processed == 0
        ), f"rolled back more than it added (processed={summary.processed})"
        assert summary.failed == 2, f"counted {summary.failed} failures for 2 notes"

    def test_the_count_never_goes_negative(self):
        summary = self._apply(self._outcomes(), write_fails=True)

        assert summary.processed >= 0
        assert "-" not in summary.message(), summary.message()


class TestAModalRunNeverWaitsForTheReviewer:
    """The wait is only coherent on a surface the user can act around.

    `ModalDialog` is `ApplicationModal`. Waiting for the user to leave the reviewer while that
    window is up waits forever: leaving the reviewer is exactly what the dialog prevents, and
    `finish()` is only reached once the write completes. The pair deadlocks Anki until it is
    killed, and the generated content only survives because quitting flushes it.

    `mw.state` stays "review" while the Browser is open on top of it, so the ordinary route in
    is not exotic: study, press `b`, select notes, generate with background mode off.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _run(self, monkeypatch, surface, count=4):
        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, count + 1)
        }
        fake = _FakeCompat(notes)
        fake.in_review = True
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(notes), summaries.append, surface=surface
        )
        return fake, list(notes), summaries

    def test_the_modal_run_writes_straight_through(self, monkeypatch):
        from omnia.plugins.smart_notes.integration.progress import ModalDialog

        fake, nids, summaries = self._run(monkeypatch, ModalDialog())

        assert fake.updated == nids, "it waited behind its own modal dialog"
        assert len(summaries) == 1, "the run never reported, so the dialog never closed"

    def test_the_dialog_is_closed_rather_than_left_over_the_reviewer(self, monkeypatch):
        from omnia.plugins.smart_notes.integration.progress import ModalDialog

        fake, _nids, _summaries = self._run(monkeypatch, ModalDialog())

        assert (
            "finish" in fake.progress
        ), "Anki was left behind a dialog nothing would ever close"

    def test_a_non_modal_run_still_waits(self, monkeypatch):
        # The courtesy is not being dropped — it is being confined to where it can be honoured.
        from omnia.plugins.smart_notes.integration.progress import Silent

        fake, _nids, _summaries = self._run(monkeypatch, Silent())

        assert fake.updated == []
        assert fake.pending_timers


class TestStopEscapesTheWaitForTheReviewer:
    """Pressing Stop during the wait has to end it, or the only way out is quitting Anki.

    The hold re-arms every two seconds for as long as the reviewer is open, and it is the one
    state where the user is watching a job that says it is waiting and doing nothing about it.
    The content is already generated and already paid for, so Stop writes it rather than
    discarding it — cancelling the WAIT, not the work.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _waiting(self, monkeypatch, count=4):
        from omnia.core.progress import JobTracker
        from omnia.plugins.smart_notes.integration.progress import BackgroundBar

        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, count + 1)
        }
        fake = _FakeCompat(notes)
        fake.in_review = True
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        tracker = JobTracker()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(notes), summaries.append, surface=BackgroundBar(tracker)
        )
        assert fake.updated == [], "the fixture did not actually wait"
        return fake, tracker, list(notes), summaries

    def test_stopping_writes_what_was_waiting(self, monkeypatch):
        fake, tracker, nids, _summaries = self._waiting(monkeypatch)

        tracker.cancel()
        fake.fire_timers()

        assert (
            fake.updated == nids
        ), "Stop threw away content that had already been paid for"

    def test_stopping_ends_the_wait(self, monkeypatch):
        fake, tracker, _nids, summaries = self._waiting(monkeypatch)

        tracker.cancel()
        fake.fire_timers()
        fake.fire_timers()

        assert len(summaries) == 1, "the run reported once, or not at all"
        assert (
            fake.pending_timers == []
        ), "it went on re-arming after being told to stop"


class TestQuittingWhileAWriteIsWaiting:
    """Deferring the write is a courtesy; losing the work is not a trade anyone would choose.

    Generated fields cost real provider money. A write-back holding off for the reviewer has
    them in hand and nowhere yet to put them, so quitting Anki — or switching the feature off —
    has to flush them first. Anki fires `profile_will_close` BEFORE it closes the collection
    (`AnkiQt.unloadProfile`), so there is still somewhere to write.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _waiting(self, monkeypatch, count=6):
        """A batch whose write-back is held off by an open reviewer."""
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module, "_WRITE_SLICE", 2)
        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": f"w{nid}", "Def": ""})
            for nid in range(1, count + 1)
        }
        fake = _FakeCompat(notes)
        fake.in_review = True
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        # Non-blocking, because a modal run does not defer at all — its dialog is the reason
        # the reviewer cannot be left, so waiting for that would never end.
        from omnia.plugins.smart_notes.integration.progress import Silent

        BatchGenerator(_generator(settings), settings).run(
            list(notes), summaries.append, surface=Silent()
        )
        assert fake.updated == [], "the fixture did not actually defer"
        return fake, list(notes), summaries

    def test_closing_writes_everything_that_was_waiting(self, monkeypatch):
        from omnia.plugins.smart_notes.integration.batch import flush_pending_writes

        fake, nids, _ = self._waiting(monkeypatch)

        flush_pending_writes()

        assert fake.updated == nids, "generated content was thrown away on close"

    def test_it_writes_even_though_the_reviewer_is_still_open(self, monkeypatch):
        # By the time this runs the user is quitting, so there is no review left to protect.
        from omnia.plugins.smart_notes.integration.batch import flush_pending_writes

        fake, nids, _ = self._waiting(monkeypatch)
        assert fake.in_review is True

        flush_pending_writes()

        assert fake.updated == nids

    def test_the_run_still_reports_back_exactly_once(self, monkeypatch):
        from omnia.plugins.smart_notes.integration.batch import flush_pending_writes

        _fake, _nids, summaries = self._waiting(monkeypatch)

        flush_pending_writes()

        assert len(summaries) == 1
        assert summaries[0].processed == 6

    def test_a_timer_that_fires_after_the_flush_does_nothing(self, monkeypatch):
        # The held callback is still on Anki's queue when the profile closes; running the
        # write twice would duplicate every note in the batch.
        from omnia.plugins.smart_notes.integration.batch import flush_pending_writes

        fake, nids, summaries = self._waiting(monkeypatch)
        flush_pending_writes()

        fake.in_review = False
        fake.fire_timers()

        assert fake.updated == nids, fake.updated
        assert len(summaries) == 1

    def test_a_finished_batch_leaves_nothing_registered(self, monkeypatch):
        # Otherwise every completed batch of the session is re-run at close.
        from omnia.plugins.smart_notes.integration.batch import (
            _PENDING_WRITES,
            flush_pending_writes,
        )

        fake, nids, _ = self._waiting(monkeypatch)
        fake.in_review = False
        fake.drain_timers()  # one tick per slice, plus the one that noticed the reviewer left
        assert fake.updated == nids

        flush_pending_writes()

        assert fake.updated == nids, "a finished batch wrote itself again on close"
        assert not _PENDING_WRITES

    def test_switching_the_feature_off_flushes_too(self, monkeypatch):
        # Same reasoning, different trigger: the work is done and paid for either way.
        fake, nids, _ = self._waiting(monkeypatch)
        from omnia.plugins.smart_notes import SmartNotesPlugin

        plugin = SmartNotesPlugin()
        with contextlib.suppress(Exception):
            # The rest of teardown needs Anki; the flush is the first thing on_disable does,
            # which is the point — it has to happen before anything can fail.
            plugin.on_disable(None)

        assert fake.updated == nids


class TestWhoseWorkARegenerationMayDestroy:
    """`Overwrite` says filled fields should be refreshed. It does not say whose work may go.

    A sentence the user typed and one Omnia generated are indistinguishable to a rule that only
    knows the field is non-empty, so `overwrite_scope` asks the second question separately.
    """

    def _settings(self, scope, **kw):
        base = {
            "note_types": [_note_type_config()],
            "regenerate_when_batching": True,
            "overwrite_scope": scope,
        }
        base.update(kw)
        return SmartNotesSettings(**base)

    def _run(self, monkeypatch, scope, existing):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": existing})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(scope)
        BatchGenerator(_generator(settings), settings).run([1], lambda _s: None)
        fake.drain_timers()
        return notes[1]["Def"]

    def test_by_default_a_regeneration_replaces_anything(self, monkeypatch):
        # What Overwrite already did. Changing that silently would be worse than a setting.
        from omnia.plugins.smart_notes.provenance import ALWAYS

        after = self._run(monkeypatch, ALWAYS, "<b>written by hand</b>")

        assert "written by hand" not in after

    def test_ours_only_leaves_a_hand_written_field_alone(self, monkeypatch):
        from omnia.plugins.smart_notes.provenance import OURS_ONLY

        after = self._run(monkeypatch, OURS_ONLY, "<b>written by hand</b>")

        assert after == "<b>written by hand</b>"

    def test_ours_only_still_refreshes_what_omnia_wrote(self, monkeypatch):
        from omnia.plugins.smart_notes.provenance import OURS_ONLY, stamp

        after = self._run(monkeypatch, OURS_ONLY, stamp("an older definition"))

        assert "an older definition" not in after

    def test_ours_only_stops_once_the_user_has_edited_it(self, monkeypatch):
        """The reason the mark carries a fingerprint rather than just saying "mine".

        The comment survives typing — it is a DOM node — so a bare mark would still authorise
        destroying the edit.
        """
        from omnia.plugins.smart_notes.provenance import OURS_ONLY, stamp

        edited = stamp("an older definition").replace("older", "older, improved by me")

        after = self._run(monkeypatch, OURS_ONLY, edited)

        assert "improved by me" in after

    def test_not_ours_protects_audio_already_paid_for(self, monkeypatch):
        from omnia.plugins.smart_notes.provenance import NOT_OURS

        after = self._run(monkeypatch, NOT_OURS, "[sound:omnia-1-Def.mp3]")

        assert after == "[sound:omnia-1-Def.mp3]"

    def test_an_empty_field_is_filled_whatever_the_scope(self, monkeypatch):
        # The setting is about OVERWRITING. The most protective scope must still not stop a
        # blank field from being generated in the first place — there is nothing to protect.
        from omnia.plugins.smart_notes.provenance import NOT_OURS, unstamp

        after = self._run(monkeypatch, NOT_OURS, "")

        assert unstamp(after) == "generated"

    def test_what_omnia_writes_carries_its_mark(self, monkeypatch):
        from omnia.plugins.smart_notes.provenance import ALWAYS, is_untouched, unstamp

        after = self._run(monkeypatch, ALWAYS, "")

        assert is_untouched(after), "an unmarked write can never be recognised later"
        assert unstamp(after) == "generated", "the reader must see only the content"


class TestReadingWhichMediaAFieldIsGivingUp:
    """Pure: given a field's old HTML, which files may be trashed."""

    def _read(self, previous):
        from omnia.plugins.smart_notes.integration.batch import superseded_media

        return superseded_media(previous)

    def test_a_sound_tag(self):
        assert self._read("[sound:omnia-1-Audio.mp3]") == ["omnia-1-Audio.mp3"]

    def test_an_image_tag_in_either_quote_style(self):
        assert self._read('<img src="omnia-1-Pic.png">') == ["omnia-1-Pic.png"]
        assert self._read("<img src='omnia-1-Pic.png'>") == ["omnia-1-Pic.png"]

    def test_another_addons_media_is_left_alone(self):
        """The reason the prefix exists.

        A real collection carries media from several sources — AwesomeTTS and HyperTTS write
        `googletts-…`, pasted images land as `paste-…`. Trashing those because a field happened
        to hold one would delete somebody's audio on the strength of a filename.
        """
        assert self._read("[sound:googletts-b4728f64-ee872c9f.mp3]") == []
        assert self._read('<img src="paste-abc123.jpg">') == []

    def test_ours_is_taken_and_theirs_is_kept_from_the_same_field(self):
        both = '[sound:googletts-aaa.mp3] <img src="omnia-9-Pic.png">'

        assert self._read(both) == ["omnia-9-Pic.png"]

    def test_an_empty_or_text_only_field_gives_up_nothing(self):
        assert self._read("") == []
        assert self._read(None) == []
        assert self._read("<b>just words</b>") == []

    def test_the_same_file_twice_is_listed_once(self):
        twice = "[sound:omnia-1-A.mp3] [sound:omnia-1-A.mp3]"

        assert self._read(twice) == ["omnia-1-A.mp3"]


class TestRegeneratingAFieldDoesNotLeaveTheOldFile:
    """`MediaManager.write_data` renames on collision rather than overwriting.

    So regenerating a field writes a NEW file and leaves the previous audio on disk referenced
    by nobody. Five regenerations of one field is five files and one useful one — which on a
    real collection came to hundreds of megabytes of audio nothing could play.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": True}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _run(self, monkeypatch, previous):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": previous})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        BatchGenerator(_generator(settings), settings).run([1], lambda _s: None)
        return fake

    def test_the_file_the_field_stops_referencing_is_trashed(self, monkeypatch):
        fake = self._run(monkeypatch, "[sound:omnia-1-Def.mp3]")

        assert fake.trashed == ["omnia-1-Def.mp3"]

    def test_a_file_a_duplicate_note_still_plays_is_kept(self, monkeypatch):
        """*Notes → Create Copy* leaves two notes pointing at one file.

        Regenerating the original stops IT referencing the audio, but the copy still plays it.
        Trashing on the strength of "this field used to point at it" silences the copy, with
        nothing on screen to say so — the user finds out the next time that card comes up.
        """
        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "[sound:omnia-1-Def.mp3]"})
        }
        fake = _FakeCompat(notes)
        fake.still_referenced = {"omnia-1-Def.mp3"}
        _patch_compat(monkeypatch, fake)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run([1], lambda _s: None)

        assert fake.trashed == []

    def test_an_identical_regeneration_keeps_the_file_the_note_now_points_at(
        self, monkeypatch
    ):
        """Regenerating does not always produce a NEW filename.

        Anki's ``add_data_to_folder_uniquely`` hashes before it renames: identical bytes come
        back under the name that already exists, and only a real collision gets a suffix.
        ``materialize`` asks for a deterministic name (``omnia-<nid>-<field>.<ext>``), so
        re-running a batch over unchanged text with a deterministic engine (piper offline,
        google_translate) gets the SAME name back. That name is then both the note's new
        reference and an entry in the superseded list, and trashing it silences exactly the
        notes the re-run had just regenerated — silently, recoverable only via Check Media.
        """
        from omnia.core import anki_compat
        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration import batch as batch_module

        trashed: list = []
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": "[sound:omnia-1-Def.mp3]"})
        # Anki's own behaviour: the name it was handed comes straight back.
        monkeypatch.setattr(anki_compat, "add_media_file", lambda name, data: name)
        monkeypatch.setattr(anki_compat, "get_note", lambda nid, col=None: note)
        monkeypatch.setattr(anki_compat, "update_notes", lambda notes, col=None: None)
        monkeypatch.setattr(
            anki_compat,
            "trash_media_files",
            lambda names, col=None: trashed.extend(names),
        )
        monkeypatch.setattr(
            anki_compat, "media_still_referenced", lambda names, col=None: set()
        )

        rule = SmartNotesFieldRule(target_field="Def", kind="tts")
        outcome = batch_module._NoteOutcome(
            1,
            materialize=batch_module.note_materializer(1),
            results=[(rule, GenerationResult("tts", data=b"same", ext="mp3"))],
        )
        settings = self._settings()
        BatchGenerator(_generator(settings), settings)._apply([outcome])

        assert (
            note["Def"] == "[sound:omnia-1-Def.mp3]"
        ), "the fixture no longer models the identical-bytes case"
        assert trashed == [], "it trashed the file the note still points at"

    def test_another_addons_file_in_that_field_is_not(self, monkeypatch):
        fake = self._run(monkeypatch, "[sound:googletts-b4728f64.mp3]")

        assert fake.trashed == []

    def test_a_field_that_held_no_media_trashes_nothing(self, monkeypatch):
        fake = self._run(monkeypatch, "")

        assert fake.trashed == []

    def test_nothing_is_trashed_when_the_write_fails(self, monkeypatch):
        """Media the note still references must survive a failed write.

        Trashing at preparation time would delete the audio a note is still pointing at, the
        moment the write behind it does not land.
        """
        import omnia.plugins.smart_notes.integration.batch as batch_module

        def refuse(notes, col=None):
            raise RuntimeError("collection is locked")

        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "[sound:omnia-1-Def.mp3]"})
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        monkeypatch.setattr(batch_module.anki_compat, "update_notes", refuse)
        settings = self._settings()

        BatchGenerator(_generator(settings), settings).run([1], lambda _s: None)

        assert fake.trashed == []

    def test_a_trash_that_fails_does_not_fail_the_batch(self, monkeypatch):
        # Disk space is not worth losing a written batch over.
        import omnia.plugins.smart_notes.integration.batch as batch_module

        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "[sound:omnia-1-Def.mp3]"})
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        monkeypatch.setattr(
            batch_module.anki_compat,
            "trash_media_files",
            lambda names, col=None: (_ for _ in ()).throw(OSError("read-only")),
        )
        settings = self._settings()
        summaries: list = []

        BatchGenerator(_generator(settings), settings).run([1], summaries.append)

        assert summaries[0].processed == 1
        assert fake.updated == [1]


class TestTheProgressThrottle:
    """Publishes are coalesced, and the LAST one escapes the coalescing.

    Load-bearing for the modal surface, which reaches the Qt main thread and would otherwise be
    handed hundreds of closures a second for a bar nobody can read that fast. The escape is what
    stops a batch that ends between ticks leaving its final frame short — which, for a
    background batch nobody is watching in real time, is the frame they actually read.
    """

    def _reporter(self, total: int):
        from omnia.plugins.smart_notes.integration.batch import _ProgressReporter
        from omnia.plugins.smart_notes.integration.progress import ProgressSurface

        class Recorder(ProgressSurface):
            def __init__(self):
                self.published: list = []

            def publish(self, done, total):
                self.published.append((done, total))

        surface = Recorder()
        return _ProgressReporter(total, surface), surface

    def test_it_coalesces_the_middle_of_a_run(self, monkeypatch):
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module.time, "monotonic", lambda: 3.5)
        monkeypatch.setattr(batch_module, "_PROGRESS_INTERVAL_SECONDS", 3600.0)
        reporter, surface = self._reporter(10)

        reporter.advance(1)  # the first always publishes
        reporter.advance(1)
        reporter.advance(1)

        assert surface.published == [(1, 10)], surface.published

    def test_the_last_unit_publishes_however_recently_the_last_one_did(
        self, monkeypatch
    ):
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module.time, "monotonic", lambda: 3.5)
        monkeypatch.setattr(batch_module, "_PROGRESS_INTERVAL_SECONDS", 3600.0)
        reporter, surface = self._reporter(3)
        reporter.advance(1)

        reporter.advance(1)
        reporter.advance(1)

        assert surface.published[-1] == (3, 3), surface.published

    def test_the_first_update_publishes_on_a_clock_that_has_just_started(
        self, monkeypatch
    ):
        """`time.monotonic()` counts from an arbitrary origin — on Linux, boot.

        Seeding "last published" with 0.0 and subtracting made the first update's age depend on
        how long the machine had been up. On a long-running box it was huge and everything
        worked; on a fresh CI runner it was a few seconds, the first publish was throttled away,
        and a batch showed nothing until its final note. This is that machine.
        """
        from omnia.plugins.smart_notes.integration import batch as batch_module

        monkeypatch.setattr(batch_module.time, "monotonic", lambda: 3.5)
        monkeypatch.setattr(batch_module, "_PROGRESS_INTERVAL_SECONDS", 3600.0)
        reporter, surface = self._reporter(10)

        reporter.advance(1)

        assert surface.published == [(1, 10)]

    def test_advancing_by_nothing_publishes_nothing(self):
        reporter, surface = self._reporter(4)

        reporter.advance(0)

        assert surface.published == []


class TestTheBatchReportsToWhicheverSurfaceItWasGiven:
    """The runner calls four methods and never asks which surface it has.

    That is what let the background mode arrive without touching the cohort/round/wave logic —
    and it is the property worth pinning, because the alternative (a ``show_progress`` boolean
    branched on in five places) is what this replaced.
    """

    def _settings(self, **kw) -> SmartNotesSettings:
        base = {"note_types": [_note_type_config()], "regenerate_when_batching": False}
        base.update(kw)
        return SmartNotesSettings(**base)

    def _recorder(self):
        from omnia.plugins.smart_notes.integration.progress import ProgressSurface

        class Recorder(ProgressSurface):
            def __init__(self):
                self.calls: list = []
                self.stop = False

            def start(self, total):
                self.calls.append(("start", total))

            def publish(self, done, total):
                self.calls.append(("publish", done, total))

            def finish(self):
                self.calls.append(("finish",))

            def cancelled(self):
                return self.stop

        return Recorder()

    def test_it_is_told_the_size_the_run_is_starting(self, monkeypatch):
        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}),
            2: _FakeNote(2, "Basic", {"Word": "dog", "Def": ""}),
        }
        _patch_compat(monkeypatch, _FakeCompat(notes))
        surface = self._recorder()

        BatchGenerator(self._generator(), self._settings()).run(
            [1, 2], lambda _s: None, surface=surface
        )

        assert surface.calls[0] == ("start", 2)

    def test_the_count_reaches_the_surface(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        _patch_compat(monkeypatch, _FakeCompat(notes))
        surface = self._recorder()

        BatchGenerator(self._generator(), self._settings()).run(
            [1], lambda _s: None, surface=surface
        )

        assert ("publish", 1, 1) in surface.calls

    def test_it_is_always_told_the_run_is_over(self, monkeypatch):
        # In a `finally`. The modal surface's start() bumped Anki's GLOBAL progress refcount,
        # and never finishing leaks it for the session — in every add-on, not just this one.
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        _patch_compat(monkeypatch, _FakeCompat(notes))
        surface = self._recorder()

        BatchGenerator(self._generator(), self._settings()).run(
            [1], lambda _s: None, surface=surface
        )

        assert surface.calls[-1] == ("finish",)

    def test_a_surface_that_says_stop_stops_the_run(self, monkeypatch):
        notes = {
            nid: _FakeNote(nid, "Basic", {"Word": "w", "Def": ""})
            for nid in range(1, 5)
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        surface = self._recorder()
        surface.stop = True
        summaries: list = []

        BatchGenerator(self._generator(), self._settings()).run(
            [1, 2, 3, 4], summaries.append, surface=surface
        )

        assert fake.updated == [], "it generated after being told to stop"
        assert summaries[0].cancelled is True

    def test_the_default_is_still_the_modal_dialog(self, monkeypatch):
        # Every caller that wants otherwise says so. A default of "background" would silently
        # change what an existing entry point does.
        from omnia.plugins.smart_notes.integration import batch as batch_module
        from omnia.plugins.smart_notes.integration.progress import ModalDialog

        seen: list = []
        monkeypatch.setattr(
            batch_module, "ModalDialog", lambda: seen.append(1) or ModalDialog()
        )
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        _patch_compat(monkeypatch, _FakeCompat(notes))

        BatchGenerator(self._generator(), self._settings()).run([1], lambda _s: None)

        assert seen, "the default surface was not the dialog"

    def _generator(self):
        return _generator(self._settings())


class TestBatchGeneratorDisabledRules:
    def test_disabled_fields_are_skipped_in_batch(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config(enabled=False)],
            regenerate_when_batching=False,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        # No enabled, generatable field → empty plan → nothing happens.
        assert summaries[0].processed == 0
        assert fake.progress == []


class TestBatchGeneratorDeckScope:
    def test_note_outside_deck_scope_is_skipped(self, monkeypatch):
        # The note's only card is in deck 9, but the config is scoped to deck 1.
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}, decks=(9,))}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config(decks=[1])],
            regenerate_when_batching=False,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].processed == 0
        assert summaries[0].skipped == 1
        assert fake.updated == []
        assert fake.progress == []  # no plan → progress never opened

    def test_note_inside_deck_scope_is_processed(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}, decks=(1,))}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config(decks=[1])],
            regenerate_when_batching=False,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].processed == 1
        assert fake.updated == [1]

    def test_empty_decks_processes_any_deck(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}, decks=(42,))}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config(decks=[])],
            regenerate_when_batching=False,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].processed == 1
        assert fake.updated == [1]


class TestBlockedSummaryDetail:
    """The summary names WHICH field was blocked and by what — a count alone is not actionable."""

    def test_names_the_blocked_field_and_its_missing_prerequisite(self):
        summary = BatchSummary(
            processed=0,
            blocked=1,
            blocked_examples=["Word (audio filename) needs Word (audio)"],
        )
        assert summary.message() == (
            "Processed 0 note(s), 1 blocked — missing prerequisites "
            "(Word (audio filename) needs Word (audio))."
        )

    def test_caps_the_named_examples(self):
        summary = BatchSummary(
            blocked=9, blocked_examples=["a needs b", "c needs d", "e needs f"]
        )
        message = summary.message()
        assert "a needs b; c needs d" in message
        assert "e needs f" not in message  # bounded so the tooltip stays readable

    def test_without_examples_falls_back_to_the_plain_count(self):
        assert BatchSummary(blocked=2).message() == (
            "Processed 0 note(s), 2 blocked — missing prerequisites."
        )


class TestEmptyNoteTracking:
    """Notes that generation tried and produced nothing for, so a clip can be discarded."""

    def _outcome(self, nid, **kw):
        from omnia.plugins.smart_notes.integration.batch import _NoteOutcome

        return _NoteOutcome(nid, **kw)

    def _apply(self, outcomes):
        """Count these outcomes without writing anything.

        These tests are about the SUMMARY, not the collection, and they build a generator with
        no Anki behind it — so the persist step is stubbed out rather than being given a fake to
        talk to. Preparation is left alone: whether a note came out with content in it is part
        of what is being counted.
        """
        gen = BatchGenerator.__new__(BatchGenerator)
        gen._persist = lambda prepared, summary: None  # type: ignore[method-assign]
        return gen._apply(outcomes)

    def test_a_note_with_no_results_is_recorded(self):
        summary = self._apply([self._outcome(11, blocked=2)])
        assert summary.empty_note_ids == [11]

    def test_a_note_that_generated_something_is_not_recorded(self, monkeypatch):
        monkeypatch.setattr(
            BatchGenerator,
            "_prepare_note",
            lambda self, o, **kw: _PreparedNote(_FakeNote(o.nid, "Basic", {}), True),
        )
        summary = self._apply([self._outcome(12, results=[("rule", "result")])])
        assert summary.empty_note_ids == []

    def test_a_hard_failure_is_NOT_recorded(self):
        # Generation raising is transient; discarding the capture over a provider hiccup would
        # lose the user's work, so a failed note is kept for a retry.
        summary = self._apply([self._outcome(13, failed=True)])
        assert summary.empty_note_ids == []

    def test_several_empties_are_all_recorded(self):
        summary = self._apply([self._outcome(1), self._outcome(2, blocked=1)])
        assert summary.empty_note_ids == [1, 2]


class TestErroredNotesAreKeptForRetry:
    """A note whose every field ERRORED is kept, not offered to the clip discarder.

    ``empty_note_ids`` means "we tried and there was nothing to make", and the gateway deletes
    those clips. A note that generated nothing because a provider was down establishes no such
    thing — one retry would fill it — so it belongs in its own list. It used to land in
    ``empty_note_ids`` and be deleted, which lost the capture over a transient outage; only a
    whole-note failure was spared.
    """

    def _outcome(self, nid, **kw):
        from omnia.plugins.smart_notes.integration.batch import _NoteOutcome

        return _NoteOutcome(nid, **kw)

    def _apply(self, outcomes):
        """Count these outcomes without writing anything.

        These tests are about the SUMMARY, not the collection, and they build a generator with
        no Anki behind it — so the persist step is stubbed out rather than being given a fake to
        talk to. Preparation is left alone: whether a note came out with content in it is part
        of what is being counted.
        """
        gen = BatchGenerator.__new__(BatchGenerator)
        gen._persist = lambda prepared, summary: None  # type: ignore[method-assign]
        return gen._apply(outcomes)

    def test_an_all_errored_note_is_kept_not_discarded(self):
        summary = self._apply([self._outcome(21, field_failures=2)])

        assert summary.empty_note_ids == []  # never handed to the discarder
        assert summary.errored_note_ids == [21]

    def test_a_partly_errored_note_is_still_kept(self):
        summary = self._apply([self._outcome(22, blocked=1, field_failures=1)])

        assert summary.empty_note_ids == []
        assert summary.errored_note_ids == [22]

    def test_a_declined_note_is_still_discardable(self):
        # "Every tool declined" IS "nothing to make here" — exactly what discarding is for.
        summary = self._apply([self._outcome(23, unfilled=2)])

        assert summary.empty_note_ids == [23]
        assert summary.errored_note_ids == []

    def test_the_summary_says_why_notes_were_kept(self):
        summary = self._apply([self._outcome(24, field_failures=1)])

        assert summary.message() == (
            "Processed 0 note(s), 1 field error(s), kept 1 note(s) for retry."
        )

    def test_a_clean_run_says_nothing_about_retries(self, monkeypatch):
        monkeypatch.setattr(
            BatchGenerator,
            "_prepare_note",
            lambda self, o, **kw: _PreparedNote(_FakeNote(o.nid, "Basic", {}), True),
        )
        summary = self._apply([self._outcome(25, results=[("rule", "result")])])

        assert summary.errored_note_ids == []
        assert "retry" not in summary.message()


class TestToolChainCounters:
    """The two counters a tool chain adds to the summary (plan 4.3 + graft #5).

    ``unfilled`` separates "every tool declined" from a real ``field_failures`` error, and
    ``tool_fallbacks`` makes a deterministic first tool that quietly stopped matching — and is
    therefore paying the LLM on every note — visible outside the log.
    """

    def _apply(self, outcomes):
        """Count these outcomes without writing anything.

        These tests are about the SUMMARY, not the collection, and they build a generator with
        no Anki behind it — so the persist step is stubbed out rather than being given a fake to
        talk to. Preparation is left alone: whether a note came out with content in it is part
        of what is being counted.
        """
        gen = BatchGenerator.__new__(BatchGenerator)
        gen._persist = lambda prepared, summary: None  # type: ignore[method-assign]
        return gen._apply(outcomes)

    def _outcome(self, nid, **kw):
        from omnia.plugins.smart_notes.integration.batch import _NoteOutcome

        return _NoteOutcome(nid, **kw)

    def _rule(self, *tools: str):
        from omnia.plugins.smart_notes.config import (
            CompiledToolSpec,
            SmartNotesFieldRule,
        )

        return SmartNotesFieldRule(
            target_field="Def",
            tools=tuple(CompiledToolSpec(name=name) for name in tools),
        )

    def _result(self, tool: str):
        from omnia.plugins.smart_notes.engine import GenerationResult

        return GenerationResult("text", text="x", tool=tool)

    def _generate_one(self, results, failed):
        """Run one note through the real cohort runner against a canned per-field outcome."""
        from omnia.plugins.smart_notes.integration.batch import _NotePlan

        gen = BatchGenerator(
            _CannedService(results, failed=failed), SmartNotesSettings()
        )
        outcomes = gen._run_cohort(
            [_NotePlan(1, _note_type_config(), {})], force_overwrite=False
        )
        return outcomes[0]

    def test_a_declined_chain_counts_as_unfilled_not_as_an_error(self):
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one(
            [], [FailedField("Def", "cloze: no match", "unproductive")]
        )

        assert (outcome.field_failures, outcome.unfilled) == (0, 1)
        # The count alone sent the reader looking through a note type's twenty fields for a
        # tool they were never told the name of. The trace is the only actionable half.
        assert self._apply([outcome]).message() == (
            "Processed 0 note(s), 1 field(s) had no applicable tool (Def — cloze: no match)."
        )

    def test_a_broken_chain_still_counts_as_a_field_error(self):
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one([], [FailedField("Def", "ai: HTTP 401", "error")])

        assert (outcome.field_failures, outcome.unfilled) == (1, 0)
        assert (
            "1 field error(s) (Def — ai: HTTP 401)" in self._apply([outcome]).message()
        )

    def test_a_broken_field_and_a_declined_one_are_reported_apart(self):
        # The two halves used to collapse into two bare counts, which read as one problem.
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one(
            [],
            [
                FailedField("Def", "ai: HTTP 401", "error"),
                FailedField("Audio", "cloze_audio: nothing to hide", "unproductive"),
            ],
        )

        message = self._apply([outcome]).message()

        assert "1 field error(s) (Def — ai: HTTP 401)" in message
        assert (
            "1 field(s) had no applicable tool (Audio — cloze_audio: nothing to hide)"
            in message
        )

    def test_an_unproductive_field_is_named_even_behind_two_errors(self):
        # The examples are picked per KIND. Slicing the failures first (the obvious way to
        # bound them) let two errored fields use up the budget and left the declined one
        # counted but unnamed — the exact report that sent the user hunting.
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one(
            [],
            [
                FailedField("A", "ai: HTTP 401", "error"),
                FailedField("B", "ai: HTTP 401", "error"),
                FailedField("C", "cloze: no match", "unproductive"),
            ],
        )

        assert "(C — cloze: no match)" in self._apply([outcome]).message()

    def test_the_same_failure_on_many_notes_is_named_once(self):
        # A batch is many notes of ONE type, so the same field fails the same way every time.

        summary = self._apply(
            [
                self._outcome(
                    nid,
                    unfilled=1,
                    unfilled_examples=["Def — cloze: no match"],
                )
                for nid in (1, 2, 3)
            ]
        )

        assert summary.unfilled_examples == ["Def — cloze: no match"]
        assert "3 field(s) had no applicable tool (Def — cloze: no match)" in (
            summary.message()
        )

    def test_a_long_provider_body_is_clipped_to_tooltip_length(self):
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one(
            [], [FailedField("Def", "ai: " + "x" * 400, "error")]
        )

        (example,) = self._apply([outcome]).error_examples
        assert len(example) < 120 and example.endswith("…")

    def test_a_note_whose_fields_all_declined_is_not_counted_as_skipped(self):
        from omnia.plugins.smart_notes.engine import FailedField

        summary = self._apply(
            [self._generate_one([], [FailedField("Def", "no match", "unproductive")])]
        )

        assert summary.skipped == 0
        assert summary.empty_note_ids == [1]

    def test_a_later_tool_producing_counts_as_a_fallback(self, monkeypatch):
        monkeypatch.setattr(
            BatchGenerator,
            "_prepare_note",
            lambda self, o, **kw: _PreparedNote(_FakeNote(o.nid, "Basic", {}), True),
        )

        outcome = self._generate_one(
            [(self._rule("cloze", "ai"), self._result("ai"))], []
        )

        assert outcome.tool_fallbacks == 1
        assert (
            "1 field(s) fell back to a later tool" in self._apply([outcome]).message()
        )

    def test_the_first_tool_producing_is_not_a_fallback(self, monkeypatch):
        monkeypatch.setattr(
            BatchGenerator,
            "_prepare_note",
            lambda self, o, **kw: _PreparedNote(_FakeNote(o.nid, "Basic", {}), True),
        )

        outcome = self._generate_one(
            [(self._rule("cloze", "ai"), self._result("cloze"))], []
        )

        assert outcome.tool_fallbacks == 0
        assert self._apply([outcome]).message() == "Processed 1 note(s)."

    def test_an_unstamped_result_never_counts(self, monkeypatch):
        # Nothing in the legacy path stamps a tool; the counter must stay silent, not guess.
        monkeypatch.setattr(
            BatchGenerator,
            "_prepare_note",
            lambda self, o, **kw: _PreparedNote(_FakeNote(o.nid, "Basic", {}), True),
        )

        outcome = self._generate_one([(self._rule("ai"), self._result(""))], [])

        assert outcome.tool_fallbacks == 0


class TestNoteMaterializer:
    """The memoisation is the load-bearing half of the media-chaining fix.

    Its job is that the filename handed to the generation chain is the one the note ends up
    referencing. `materialize` adds bytes to the media folder and Anki renames on collision, so
    a second call for the same field would return a DIFFERENT name — and a tool that already
    extracted the first one would be pointing at a file the note does not reference.

    The previous version of this suite asserted only that the ENGINE invoked a hand-rolled
    callback once, which was true before the fix as well: the engine reaches that line once per
    rule regardless. These call `note_materializer` itself.
    """

    @staticmethod
    def _rule(field="Audio"):
        from types import SimpleNamespace

        return SimpleNamespace(target_field=field)

    def test_the_same_field_is_written_once_and_answers_the_same(self, monkeypatch):
        from omnia.core import anki_compat
        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration.batch import note_materializer

        writes: list[str] = []
        monkeypatch.setattr(
            anki_compat,
            "add_media_file",
            lambda name, data: writes.append(name) or name,
        )
        materialize_once = note_materializer(7)
        rule = self._rule()
        result = GenerationResult("tts", data=b"aa", ext="mp3")

        first = materialize_once(rule, result)
        second = materialize_once(rule, result)

        assert first == second
        assert len(writes) == 1, writes

    def test_a_second_field_is_its_own_file(self, monkeypatch):
        # Memoising per NOTE rather than per field would collapse two fields into one clip.
        from omnia.core import anki_compat
        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration.batch import note_materializer

        writes: list[str] = []
        monkeypatch.setattr(
            anki_compat,
            "add_media_file",
            lambda name, data: writes.append(name) or name,
        )
        materialize_once = note_materializer(7)
        result = GenerationResult("tts", data=b"aa", ext="mp3")

        a = materialize_once(self._rule("Audio"), result)
        b = materialize_once(self._rule("Example"), result)

        assert a != b
        assert len(writes) == 2, writes

    def test_generation_and_the_write_share_one_materializer(self):
        """The regression the reviewer showed stays green without this.

        Swapping `outcome.materialize(...)` for a fresh `note_materializer(outcome.nid)(...)`
        at the write passes every other test in the suite while restoring the bug in its worse
        form — a second media write under a renamed file. Only identity catches that.
        """
        from omnia.plugins.smart_notes.integration import batch as batch_module

        service = _CannedService()
        settings = SmartNotesSettings(note_types=[])
        generator = BatchGenerator(service, settings)
        plan = batch_module._NotePlan(
            nid=7,
            config=SmartNotesNoteTypeConfig(note_type="T", base_field="Front"),
            fields={"Front": "x"},
        )

        outcomes = generator._run_cohort([plan], force_overwrite=False)

        assert service.materializers == [outcomes[0].materialize]

    def test_the_write_reuses_the_generation_materializer(self, monkeypatch):
        """Catches the swap the identity test above cannot see.

        Replacing `outcome.materialize(...)` at the write with a FRESH
        `note_materializer(outcome.nid)(...)` leaves every other test green while restoring the
        bug in its worse form: the same bytes added a second time, Anki renaming on collision,
        and the filename a tool already extracted pointing at a file the note does not
        reference. Counting the media writes across generation AND the write is what sees it.
        """
        from omnia.core import anki_compat
        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration import batch as batch_module

        writes: list[str] = []
        monkeypatch.setattr(
            anki_compat,
            "add_media_file",
            lambda name, data: writes.append(name) or name,
        )
        note: dict[str, str] = {"Audio": ""}
        monkeypatch.setattr(anki_compat, "get_note", lambda nid: note)
        monkeypatch.setattr(anki_compat, "update_note", lambda n: None)

        materialize_once = batch_module.note_materializer(7)
        rule = self._rule("Audio")
        result = GenerationResult("tts", data=b"aa", ext="mp3")
        during_generation = materialize_once(rule, result)  # what the chain saw

        outcome = batch_module._NoteOutcome(
            7, materialize=materialize_once, results=[(rule, result)]
        )
        settings = SmartNotesSettings(note_types=[])
        BatchGenerator(object(), settings)._prepare_note(
            outcome, counted_as_processed=True
        )

        assert (
            len(writes) == 1
        ), writes  # generation wrote it; the write must not write again
        assert note["Audio"] == during_generation  # and must agree on the name

    def test_a_text_result_needs_no_materializer_at_all(self):
        """A note of pure text must not be punished for carrying no materializer.

        The first version of `_unmaterialized` raised for ANY kind, so an outcome built without
        one — every default construction, including the three helpers in this file — turned a
        perfectly writable text result into a note swallowed by the broad `except Exception`,
        written nowhere, and counted as FAILED. That is the same "no output, no error" shape
        this change set out to remove, one layer down.
        """
        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration.batch import _unmaterialized

        assert (
            _unmaterialized(self._rule(), GenerationResult("text", text="hello"))
            == "hello"
        )
        assert _unmaterialized(self._rule(), GenerationResult("text", text=None)) == ""

    def test_media_without_a_materializer_still_refuses(self):
        # Bytes with nowhere to store them is a real bug; silence there would hide it.
        import pytest as _pytest

        from omnia.plugins.smart_notes.engine.generators import GenerationResult
        from omnia.plugins.smart_notes.integration.batch import _unmaterialized

        with _pytest.raises(RuntimeError, match="no materializer"):
            _unmaterialized(self._rule(), GenerationResult("tts", data=b"x", ext="mp3"))
