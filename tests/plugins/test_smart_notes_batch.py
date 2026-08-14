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

    def test_cancel_stops_before_finishing(self, monkeypatch):
        notes = {
            n: _FakeNote(n, "Basic", {"Word": "w", "Def": ""}) for n in range(1, 13)
        }
        # Cancel is polled once per chunk (size 5); allow the first poll, cancel the second.
        fake = _FakeCompat(notes, cancel_after=1)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(range(1, 13)), summaries.append
        )
        assert summaries[0].cancelled is True
        # Only the first chunk (5) was generated before the cancel was honoured.
        assert summaries[0].processed == 5


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
        """Run ``_generate_one`` against a service stub returning the given per-field outcome."""
        from omnia.plugins.smart_notes.integration.batch import _NotePlan

        class _Service:
            def generate_note(self, config, fields, **kwargs):
                return results, [], failed

        gen = BatchGenerator(_Service(), SmartNotesSettings())
        return gen._generate_one(
            _NotePlan(1, _note_type_config(), {}), force_overwrite=False
        )

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
