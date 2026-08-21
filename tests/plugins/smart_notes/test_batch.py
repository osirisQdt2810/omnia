"""Tests for the smart_notes batch generator (the cancellable, counted batch glue).

The batch runner is Anki glue, but its orchestration — dedupe, per-note rule selection,
chunked generation, cancel handling, and the success/fail/skip summary — is exercised here by
faking the ``anki_compat`` seams it calls (collection reads, progress dialog, media writes).
The ``run_in_background`` seam is the synchronous QueryOp stub from ``conftest``, so the whole
flow runs inline.
"""

from __future__ import annotations

from conftest import FakeLLMProvider

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration.batch import BatchGenerator, BatchSummary


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

    # collection
    def get_note(self, nid, col=None):
        return self._notes[nid]

    def note_deck_ids(self, note, col=None):
        return [int(c.did) for c in note.cards()]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

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

    # threading
    def run_on_main(self, callback):
        self.run_on_main_calls += 1
        callback()

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:  # mirror QueryOp routing
            if on_failure:
                on_failure(exc)


def _patch_compat(monkeypatch, fake: _FakeCompat) -> None:
    import omnia.plugins.smart_notes.integration.batch as batch

    for name in (
        "get_note",
        "note_deck_ids",
        "update_note",
        "add_media_file",
        "progress_start",
        "progress_update",
        "progress_finish",
        "progress_was_cancelled",
        "run_on_main",
        "run_in_background",
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
        assert notes[1]["Def"] == "generated"

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
        assert notes[1]["Def"] == "generated"

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
            assert notes[nid]["Def"], f"note {nid} lost its first level"
            assert notes[nid]["Extra"], f"note {nid} lost its second level"
        # And every touched note is accounted for — none silently in no bucket.
        assert summaries[0].processed == len(touched)


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
        gen = BatchGenerator.__new__(BatchGenerator)
        return gen._apply(outcomes)

    def test_a_note_with_no_results_is_recorded(self):
        summary = self._apply([self._outcome(11, blocked=2)])
        assert summary.empty_note_ids == [11]

    def test_a_note_that_generated_something_is_not_recorded(self, monkeypatch):
        monkeypatch.setattr(BatchGenerator, "_write_note", lambda self, o: True)
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
        gen = BatchGenerator.__new__(BatchGenerator)
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
        monkeypatch.setattr(BatchGenerator, "_write_note", lambda self, o: True)
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
        gen = BatchGenerator.__new__(BatchGenerator)
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
        assert self._apply([outcome]).message() == (
            "Processed 0 note(s), 1 field(s) had no applicable tool."
        )

    def test_a_broken_chain_still_counts_as_a_field_error(self):
        from omnia.plugins.smart_notes.engine import FailedField

        outcome = self._generate_one([], [FailedField("Def", "ai: HTTP 401", "error")])

        assert (outcome.field_failures, outcome.unfilled) == (1, 0)
        assert "1 field error(s)" in self._apply([outcome]).message()

    def test_a_note_whose_fields_all_declined_is_not_counted_as_skipped(self):
        from omnia.plugins.smart_notes.engine import FailedField

        summary = self._apply(
            [self._generate_one([], [FailedField("Def", "no match", "unproductive")])]
        )

        assert summary.skipped == 0
        assert summary.empty_note_ids == [1]

    def test_a_later_tool_producing_counts_as_a_fallback(self, monkeypatch):
        monkeypatch.setattr(BatchGenerator, "_write_note", lambda self, o: True)

        outcome = self._generate_one(
            [(self._rule("cloze", "ai"), self._result("ai"))], []
        )

        assert outcome.tool_fallbacks == 1
        assert (
            "1 field(s) fell back to a later tool" in self._apply([outcome]).message()
        )

    def test_the_first_tool_producing_is_not_a_fallback(self, monkeypatch):
        monkeypatch.setattr(BatchGenerator, "_write_note", lambda self, o: True)

        outcome = self._generate_one(
            [(self._rule("cloze", "ai"), self._result("cloze"))], []
        )

        assert outcome.tool_fallbacks == 0
        assert self._apply([outcome]).message() == "Processed 1 note(s)."

    def test_an_unstamped_result_never_counts(self, monkeypatch):
        # Nothing in the legacy path stamps a tool; the counter must stay silent, not guess.
        monkeypatch.setattr(BatchGenerator, "_write_note", lambda self, o: True)

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
        BatchGenerator(object(), settings)._write_note(outcome)

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
