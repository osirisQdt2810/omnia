"""Tests for LAYER 3: one provider call for the same field across K notes.

The property every test here defends is the same one: **a note is never handed another note's
content**. Batching may cost extra calls, may degrade to per-note generation, may report a field
error — but it may not misroute. So the deliberately hostile responses (one item short, items
renumbered, items reordered, items duplicated, no JSON at all) are the point of the file, not an
afterthought: a runner that zips answers onto notes by position passes every happy-path test and
fails all of these.

Zero latency throughout — these are invariant tests, not measurements. The wall-clock question
is `tests/benchmarks/smart_notes_throughput.py`'s.
"""

from __future__ import annotations

import json
import threading

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider

from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH
from omnia.core.providers import ProviderError
from omnia.core.providers.llm.base import PromptParts
from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.engine.batching import (
    SOLO_PLANNER,
    BatchShapeError,
    ChunkTask,
    FieldBatchRunner,
    FieldBudget,
    FieldWork,
    SoloTask,
    WaveTask,
    chunk_key,
    match_items,
    parse_batch_items,
    run_wave,
)
from omnia.plugins.smart_notes.integration.batch import BatchGenerator


class _BatchingLLM(FakeLLMProvider):
    """Answers a batched request from the ids it was actually sent, and counts its calls.

    ``corrupt`` is how a hostile provider is simulated: the reply is built correctly and then
    damaged in one specific, realistic way. Solo calls (``generate_cached_text``) are answered
    normally, so a test can tell a batched answer from a fallback one by its shape.
    """

    def __init__(self, corrupt: str = "", fail: Exception | None = None) -> None:
        super().__init__()
        self._corrupt = corrupt
        self._fail = fail
        self._lock = threading.Lock()
        self.batch_calls: list[list[str]] = []  # the ids sent, per batched call
        self.solo_calls: list[str] = []  # the prompts sent, per individual call

    def generate_text(self, prompt, *, system=None, temperature=None, max_tokens=None):
        with self._lock:
            self.solo_calls.append(prompt)
        return f"solo:{prompt}"

    def generate_json(
        self, parts, *, schema, system=None, temperature=None, max_tokens=None
    ):
        items = json.loads(parts.suffix)
        with self._lock:
            self.batch_calls.append([item["id"] for item in items])
        if self._fail is not None:
            raise self._fail
        if self._corrupt == "not-json":
            return "I'm sorry, I can't do that.", None
        replies = [
            {"id": item["id"], "content": f"batched:{item['values']['Word']}"}
            for item in items
        ]
        if self._corrupt == "drop-third":
            replies.pop(2)
        elif self._corrupt == "renumber":
            replies = [
                {"id": str(index), "content": reply["content"]}
                for index, reply in enumerate(replies)
            ]
        elif self._corrupt == "reorder":
            replies.reverse()
        elif self._corrupt == "duplicate-first":
            replies = [replies[0], *replies]
        elif self._corrupt == "swap-under-duplicate-id":
            # The dangerous one: the model has lost the id-to-item mapping and answers item 2
            # under item 1's id, THEN item 1 under item 1's id. Whoever keeps "the first copy"
            # writes note 2's definition onto note 1 and never notices.
            replies = [
                {"id": items[0]["id"], "content": replies[1]["content"]},
                {"id": items[0]["id"], "content": replies[0]["content"]},
                *replies[2:],
            ]
        elif self._corrupt == "collapse":
            # Every id correct, every id unique, every answer the same string.
            replies = [
                {"id": reply["id"], "content": replies[0]["content"]}
                for reply in replies
            ]
        elif self._corrupt == "collapse-partial":
            # The one a whole-chunk check cannot see: item 1's answer is copied under the ids of
            # items 2..K-1, and the LAST item is answered properly. Two distinct strings come
            # back, so "did every item answer the same?" says no and routes all K.
            replies = [
                {"id": reply["id"], "content": replies[0]["content"]}
                for reply in replies[:-1]
            ] + [replies[-1]]
        elif self._corrupt == "extra-id":
            # FIRST, not last: an invented id that arrives before any real one is where a
            # "best guess the next free slot" fallback would do its damage.
            replies.insert(0, {"id": "ffffff", "content": "batched:nobody"})
        elif self._corrupt == "empty-array":
            replies = []
        elif self._corrupt == "truncate":
            return json.dumps({"items": replies})[:-25], None
        return json.dumps({"items": replies}), None


class _StubHub:
    def __init__(self, *, llm=None, tts=None) -> None:
        self._llm = llm
        self._tts = tts

    def llm(self, *, model: str = "", image_model: str = "", provider: str = ""):
        return self._llm

    def tts(self, *, provider: str = ""):
        return self._tts

    def resolve_auto_voice(self, lang: str, *, reason: str = ""):
        return ("fake", "en-voice")


def _config(fields):
    return SmartNotesNoteTypeConfig(
        note_type="Vocab",
        base_field="Word",
        fields=[SmartNotesFieldConfig(field=name, **kw) for name, kw in fields],
    )


_TEXT_ONLY = _config(
    [("Def", dict(enabled=True, type="text", prompt="Define {{Word}} clearly."))]
)


class _FakeCard:
    def __init__(self, did: int) -> None:
        self.did = did


class _FakeNote:
    def __init__(self, nid, fields, note_type="Vocab") -> None:
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)

    def keys(self):
        return list(self._fields.keys())

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]

    def __setitem__(self, key, value):
        self._fields[key] = value

    def note_type(self):
        return {"name": self._note_type}

    def cards(self):
        return [_FakeCard(1)]


class _FakeCompat:
    def __init__(self, notes) -> None:
        self._notes = notes
        self.updated: list[int] = []
        self.media: list[str] = []

    def get_note(self, nid, col=None):
        return self._notes[nid]

    def note_deck_ids(self, note, col=None):
        return [1]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def add_media_file(self, filename, data, col=None):
        self.media.append(filename)
        return filename

    def progress_start(self, label, maximum):
        pass

    def progress_update(self, label, value, maximum):
        pass

    def progress_finish(self):
        pass

    def progress_was_cancelled(self):
        return False

    def run_on_main(self, callback):
        callback()

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:  # pragma: no cover - a test failure must be visible
            if on_failure:
                on_failure(exc)
            raise


def _patch_compat(monkeypatch, fake):
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


def _run_batch(
    monkeypatch,
    llm,
    *,
    notes=5,
    notes_per_call=10,
    config=None,
    tts=None,
    filled=None,
    **settings_kwargs,
):
    """Drive N notes through the REAL BatchGenerator and return (notes, summary, compat).

    Pins the env ceiling to the K under test. ``OMNIA_SMART_NOTES_BATCHING`` is both the off
    switch and the upper bound on the stored ``batch_notes_per_call``, so a test that set only
    the stored value would measure whatever the machine's environment happened to allow — which
    is exactly the mistake this helper exists to make impossible.
    """
    monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", str(notes_per_call))
    config = config or _TEXT_ONLY
    filled = filled or {}
    fake_notes = {
        nid: _FakeNote(
            nid,
            {
                "Word": f"w{nid}",
                **{row.field: filled.get(nid, "") for row in config.fields},
            },
        )
        for nid in range(1, notes + 1)
    }
    compat = _FakeCompat(fake_notes)
    _patch_compat(monkeypatch, compat)
    settings = SmartNotesSettings(
        note_types=[config],
        regenerate_when_batching=False,
        batch_notes_per_call=notes_per_call,
        **settings_kwargs,
    )
    service = GenerationService(_StubHub(llm=llm, tts=tts), detect_tts_language=False)
    summaries: list = []
    BatchGenerator(service, settings).run(list(range(1, notes + 1)), summaries.append)
    return fake_notes, summaries[0], compat


class TestIdMatchingNeverUsesPosition:
    """The headline property. Every case here is a response a positional runner misroutes."""

    def test_a_response_missing_one_item_never_shifts_content_onto_another_note(
        self, monkeypatch
    ):
        # K=5 in, 4 back with the THIRD omitted. Zip-by-position would give note 3 note 4's
        # definition, note 4 note 5's, and leave note 5 with nothing — silently, and written.
        llm = _BatchingLLM(corrupt="drop-third")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=5)

        assert [notes[n]["Def"] for n in (1, 2, 4, 5)] == [
            "batched:w1",
            "batched:w2",
            "batched:w4",
            "batched:w5",
        ]
        assert notes[3]["Def"] == "solo:Define w3 clearly."  # the one that fell back
        assert len(llm.batch_calls) == 1 and len(llm.solo_calls) == 1
        assert summary.processed == 5

    def test_a_reordered_response_still_lands_on_the_right_notes(self, monkeypatch):
        # Every id comes back, in reverse. Nothing falls back, and nothing is misrouted:
        # position was never consulted.
        llm = _BatchingLLM(corrupt="reorder")

        notes, _summary, _compat = _run_batch(monkeypatch, llm, notes=5)

        assert [notes[n]["Def"] for n in range(1, 6)] == [
            f"batched:w{n}" for n in range(1, 6)
        ]
        assert llm.solo_calls == []

    def test_renumbered_ids_are_discarded_not_applied(self, monkeypatch):
        # The model answers with "0".."3" instead of the tokens it was given. Every id is
        # unknown, so NOTHING is applied — the notes are generated on their own instead. An
        # answer that arrived but routed nowhere is indistinguishable from a truncated one here,
        # so it takes the same single halving before the per-note fallback.
        llm = _BatchingLLM(corrupt="renumber")

        notes, _summary, _compat = _run_batch(monkeypatch, llm, notes=4)

        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        assert [len(ids) for ids in llm.batch_calls] == [4, 2, 2]
        assert len(llm.solo_calls) == 4

    def test_every_copy_of_a_duplicated_id_is_discarded(self):
        """A repeated id discards ALL of its copies, not merely the ones after the first.

        The unsafe version kept the first copy. When the model has lost the id↔item mapping,
        the first copy is not "the good one" — here it carries note ``bb``'s answer under
        ``aa``'s id, so keeping it writes one note's content onto another, silently, while
        ``bb`` falls back to a correct solo call and the run looks clean.
        """
        by_id = {"aa": 0, "bb": 1, "cc": 2}

        matched = match_items(
            [
                ("aa", "bb's answer"),
                ("aa", "aa's answer"),
                ("cc", "cc's answer"),
            ],
            by_id,
        )

        assert matched == {2: "cc's answer"}

    def test_an_unknown_id_is_discarded(self):
        assert match_items([("nope", "content")], {"aa": 0}) == {}

    def test_a_non_string_or_blank_content_is_discarded(self):
        by_id = {"aa": 0, "bb": 1, "cc": 2}

        assert match_items([("aa", 5), ("bb", "   "), ("cc", "ok")], by_id) == {2: "ok"}


class TestResponseParsing:
    def test_a_fenced_json_response_parses(self):
        raw = '```json\n{"items": [{"id": "ab", "content": "x"}]}\n```'

        assert parse_batch_items(raw) == [("ab", "x")]

    def test_a_bare_top_level_list_parses(self):
        assert parse_batch_items('[{"id": "ab", "content": "x"}]') == [("ab", "x")]

    def test_prose_is_a_shape_error_not_a_guess(self):
        with pytest.raises(BatchShapeError):
            parse_batch_items("Sure! Here are the definitions.")

    def test_json_without_an_items_array_is_a_shape_error(self):
        with pytest.raises(BatchShapeError):
            parse_batch_items('{"result": "ok"}')


class TestAMalformedReplyCostsCallsAndNothingElse:
    """Two guards in the parse/route path that a tidy-up could delete without a test failing."""

    def test_a_non_dict_entry_in_the_array_is_skipped_not_crashed_on(self):
        """Guard: the ``isinstance(entry, dict)`` filter in :func:`parse_batch_items`.

        Without it ``entry.get`` raises ``AttributeError``, which is not a ``BatchShapeError``,
        so it escapes the parse, escapes ``_send``, escapes ``ChunkTask.run``, and ``run_wave``
        charges it to every slot the chunk owned — K field errors instead of K solo fallbacks.
        """
        pairs = parse_batch_items(
            json.dumps({"items": ["oops", {"id": "a1", "content": "hello"}]})
        )

        assert pairs == [("a1", "hello")]

    def test_an_unhashable_id_is_discarded_rather_than_looked_up(self):
        """Guard: the ``isinstance(item_id, str)`` check in :func:`match_items`.

        Its real job is not type tidiness — it is stopping ``{"id": {"a": 1}}`` from reaching
        ``by_id.get({...})``, which raises ``TypeError: unhashable type`` and takes the whole
        chunk down the same escape route as above.
        """
        matched = match_items([({"a": 1}, "x"), ("a1", "hello")], {"a1": 0})

        assert matched == {0: "hello"}


class TestNoNoteEverGetsAnotherNotesContent:
    """The six shapes a model returns when it has lost the id-to-item correspondence.

    Every one of them must cost extra calls and cost nothing else. Each test names the guard it
    defends, because each guard is one ``continue`` or one ``if`` away from being deleted by
    someone tidying up — and every one of them, removed, produces silent corruption rather than
    a failure anyone would notice.
    """

    def test_a_duplicated_id_never_writes_one_notes_content_onto_another(
        self, monkeypatch
    ):
        """Guard: ``match_items`` skips EVERY copy of a repeated id (the ``Counter`` pass).

        Delete it and note 1 is written note 2's definition, note 2 falls back to a correct
        solo call, the summary says "Processed 3" and nothing anywhere records that a note now
        holds another note's content.
        """
        llm = _BatchingLLM(corrupt="swap-under-duplicate-id")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=3)

        # Notes 1 and 2 (both touched by the duplicate) are generated on their own; note 3's
        # id was untouched, so its batched answer stands.
        assert notes[1]["Def"] == "solo:Define w1 clearly."
        assert notes[2]["Def"] == "solo:Define w2 clearly."
        assert notes[3]["Def"] == "batched:w3"
        assert summary.processed == 3
        assert len(llm.solo_calls) == 2  # the cost: two extra calls, no corruption

    def test_a_collapsed_response_is_discarded_instead_of_copied_onto_every_note(
        self, monkeypatch
    ):
        """Guard: :func:`collapsed_indexes`, on a chunk that collapsed entirely.

        Delete it and all four notes are written note 1's definition — every id correct, every
        id unique, nothing missing, zero solo calls, zero field errors. It is the only
        misroute the id discipline alone cannot see.
        """
        llm = _BatchingLLM(corrupt="collapse")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=4)

        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        assert summary.processed == 4
        assert summary.field_failures == 0

    def test_a_partially_collapsed_response_never_reaches_the_notes_it_copied_onto(
        self, monkeypatch
    ):
        """Guard: :func:`collapsed_indexes` asking the question per GROUP, not per chunk.

        The reply is well-formed and every id is correct and unique, but items 1-4 all carry
        item 1's answer and item 5 carries its own. A whole-chunk check ("did EVERY matched item
        come back with the same string?") sees two distinct strings, discards nothing, and writes
        note 1's definition onto notes 2, 3 and 4 with zero solo calls, zero field errors and a
        clean summary. Narrow the check back to the whole chunk and this test fails on the first
        assertion.
        """
        llm = _BatchingLLM(corrupt="collapse-partial")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=5)

        # The four that shared one answer are re-generated on their own...
        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        # ...and the one the model really did answer keeps its batched content: the drop is per
        # group, so a partial collapse costs four calls, not five.
        assert notes[5]["Def"] == "batched:w5"
        assert len(llm.solo_calls) == 4
        assert summary.processed == 5
        assert summary.field_failures == 0

    def test_two_notes_that_really_are_identical_still_batch(self, monkeypatch):
        """The other half of the collapse guard: identical INPUTS may give identical answers.

        Without the input comparison, a deck with duplicate words would fan out into individual
        calls forever — the guard would fire on a correct response.
        """
        llm = _BatchingLLM()
        config = _config(
            [("Def", dict(enabled=True, type="text", prompt="Define {{Word}}."))]
        )
        fake_notes = {
            nid: _FakeNote(nid, {"Word": "same", "Def": ""}) for nid in (1, 2, 3)
        }
        compat = _FakeCompat(fake_notes)
        _patch_compat(monkeypatch, compat)
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "10")
        settings = SmartNotesSettings(
            note_types=[config],
            regenerate_when_batching=False,
            batch_notes_per_call=10,
        )
        summaries: list = []
        BatchGenerator(
            GenerationService(_StubHub(llm=llm), detect_tts_language=False), settings
        ).run([1, 2, 3], summaries.append)

        assert [fake_notes[n]["Def"] for n in (1, 2, 3)] == ["batched:same"] * 3
        assert llm.solo_calls == []  # one batched call answered all three

    def test_an_id_we_never_sent_is_dropped_and_lands_on_nobody(self, monkeypatch):
        """Guard: the ``by_id.get(item_id) is None`` skip in :func:`match_items`."""
        llm = _BatchingLLM(corrupt="extra-id")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=3)

        assert [notes[n]["Def"] for n in (1, 2, 3)] == [
            f"batched:w{n}" for n in (1, 2, 3)
        ]
        assert "nobody" not in json.dumps(
            {n: notes[n]["Def"] for n in (1, 2, 3)}
        )  # the invented id reached no note
        assert llm.solo_calls == []
        assert summary.processed == 3

    def test_an_unknown_id_set_routes_nothing_and_every_note_falls_back(
        self, monkeypatch
    ):
        """Guard: the same skip, when EVERY id is unknown (a renumbering model)."""
        llm = _BatchingLLM(corrupt="renumber")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=4)

        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        assert summary.processed == 4

    def test_an_empty_items_array_falls_back_and_writes_nothing_wrong(
        self, monkeypatch
    ):
        """Guard: an answer that parsed but routed nothing takes the unusable-response rung.

        The failure mode without it is not corruption but silence — K notes with no content and
        no error, which downstream reads as "nothing to make here" and uses to DELETE clips.
        """
        llm = _BatchingLLM(corrupt="empty-array")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=4)

        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        assert summary.empty_note_ids == []
        assert summary.processed == 4

    def test_a_truncated_body_is_a_shape_error_not_a_partial_apply(self, monkeypatch):
        """Guard: :func:`parse_batch_items` raising rather than salvaging a prefix.

        A truncated array is where zip-by-position does its worst damage, because the JSON that
        DID arrive is well-formed up to the cut. Nothing may be applied from it.
        """
        llm = _BatchingLLM(corrupt="truncate")

        notes, summary, _compat = _run_batch(monkeypatch, llm, notes=4)

        assert [notes[n]["Def"] for n in range(1, 5)] == [
            f"solo:Define w{n} clearly." for n in range(1, 5)
        ]
        assert summary.processed == 4
        assert summary.field_failures == 0


class TestTheEnvKnobDecides:
    """``OMNIA_SMART_NOTES_BATCHING`` is K: ``-1`` is off, and it caps the stored setting."""

    def test_minus_one_is_off_however_high_the_stored_k_is(self, monkeypatch):
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "-1")
        settings = SmartNotesSettings(batch_notes_per_call=10)

        assert settings.notes_per_call() == 1

    def test_the_default_is_ten_when_nothing_is_set(self, monkeypatch):
        """The shipped configuration: K = 10, from the OUTPUT BUDGET (see envs.py).

        Briefly 20, on the strength of one live session that had K = 20 finishing sooner. It
        does not reproduce — a second session had K = 20 tied with ungrouped and K = 10 2.2x
        slower — so the latency argument cannot carry a default in either direction. 10 is what
        the argument that does NOT depend on the timing study gives: a chunk asks for K answers
        inside one completion, the binding field runs ~677 output tokens at its longest, and
        8192/677 ~= 12. Both sessions' rows are in ``tests/benchmarks/data/``.
        """
        monkeypatch.delenv("OMNIA_SMART_NOTES_BATCHING", raising=False)

        assert SmartNotesSettings().notes_per_call() == 10

    def test_the_stored_k_is_preserved_while_the_knob_says_off(self, monkeypatch):
        """Off must not rewrite the user's number — raising the ceiling restores their K."""
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "-1")
        settings = SmartNotesSettings(batch_notes_per_call=8)

        assert settings.batch_notes_per_call == 8
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "10")
        assert settings.notes_per_call() == 8

    def test_the_knob_caps_a_stored_k_it_does_not_allow(self, monkeypatch):
        """The env has the last word in both directions — including down, without a sync."""
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "3")

        assert SmartNotesSettings(batch_notes_per_call=20).notes_per_call() == 3

    def test_a_user_may_ask_for_less_than_the_knob_allows(self, monkeypatch):
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "10")

        assert SmartNotesSettings(batch_notes_per_call=2).notes_per_call() == 2

    def test_with_the_knob_off_a_run_takes_the_pre_layer_3_code_path(self, monkeypatch):
        """Not "batching at width one" — the batching code is never constructed at all.

        Asserted through the real runner: a K of 10 is stored, and every note still makes its
        own ordinary call, with no batched request anywhere.
        """
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "-1")
        llm = _BatchingLLM()
        fake_notes = {
            nid: _FakeNote(nid, {"Word": f"w{nid}", "Def": ""}) for nid in range(1, 6)
        }
        _patch_compat(monkeypatch, _FakeCompat(fake_notes))
        settings = SmartNotesSettings(
            note_types=[_TEXT_ONLY],
            regenerate_when_batching=False,
            batch_notes_per_call=10,
        )
        summaries: list = []
        BatchGenerator(
            GenerationService(_StubHub(llm=llm), detect_tts_language=False), settings
        ).run(list(range(1, 6)), summaries.append)

        assert llm.batch_calls == []  # nothing was ever grouped
        assert len(llm.solo_calls) == 5
        assert [fake_notes[n]["Def"] for n in range(1, 6)] == [
            f"solo:Define w{n} clearly." for n in range(1, 6)
        ]

    def test_the_planner_is_the_solo_one_when_the_knob_is_off(self, monkeypatch):
        """The seam itself: no envelope builder is even constructed."""
        monkeypatch.setenv("OMNIA_SMART_NOTES_BATCHING", "-1")
        settings = SmartNotesSettings(batch_notes_per_call=10)
        service = GenerationService(_StubHub(llm=_BatchingLLM()))

        assert service.batch_planner(notes_per_call=settings.notes_per_call()) is (
            SOLO_PLANNER
        )


class TestFallbackLadder:
    def test_an_unparseable_response_halves_once_then_falls_back_per_note(
        self, monkeypatch
    ):
        # The exact call sequence is the assertion: one chunk of 8, two of 4, then 8 individual
        # calls. A ladder that halved recursively would issue far more.
        llm = _BatchingLLM(corrupt="not-json")

        notes, _summary, _compat = _run_batch(
            monkeypatch, llm, notes=8, notes_per_call=8
        )

        assert [len(ids) for ids in llm.batch_calls] == [8, 4, 4]
        assert len(llm.solo_calls) == 8
        assert notes[1]["Def"] == "solo:Define w1 clearly."

    def test_a_provider_error_goes_straight_to_per_note_without_halving(
        self, monkeypatch
    ):
        # Not a shape problem: an individual call restores the retry policy and per-field
        # isolation, and halving would only spend another failing call first.
        llm = _BatchingLLM(fail=ProviderError("HTTP 500", status_code=500))

        notes, _summary, _compat = _run_batch(
            monkeypatch, llm, notes=6, notes_per_call=6
        )

        assert [len(ids) for ids in llm.batch_calls] == [6]
        assert len(llm.solo_calls) == 6
        assert notes[4]["Def"] == "solo:Define w4 clearly."

    def test_a_429_does_not_fan_out_and_marks_every_note_kind_error(self, monkeypatch):
        """A rate limit must not become K retries — and must not delete anyone's notes.

        ``empty_note_ids`` is what the integration gateway feeds to its clip discarder, so a
        chunk failure recorded as "nothing to make here" would turn a provider outage into
        deleted captures. ``kind="error"`` is what keeps these notes out of it.
        """
        llm = _BatchingLLM(fail=ProviderError("HTTP 429", status_code=429))

        notes, summary, _compat = _run_batch(
            monkeypatch, llm, notes=6, notes_per_call=6
        )

        assert [len(ids) for ids in llm.batch_calls] == [6]
        assert llm.solo_calls == []  # no fan-out
        assert summary.field_failures == 6 and summary.unfilled == 0
        assert summary.empty_note_ids == []
        assert sorted(summary.errored_note_ids) == [1, 2, 3, 4, 5, 6]
        assert all(notes[n]["Def"] == "" for n in range(1, 7))

    def test_a_half_that_fails_again_falls_back_rather_than_halving_twice(self):
        # Driven at the task level so the halving arithmetic is visible: 4 -> 2 + 2 -> solos.
        llm = _BatchingLLM(corrupt="not-json")
        works = _works(4, llm)
        task = ChunkTask(
            range(4),
            works,
            providers=_StubHub(llm=llm),
            budget=FieldBudget(4, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}}"),
        )

        outcomes = task.run()

        assert [len(ids) for ids in llm.batch_calls] == [4, 2, 2]
        assert len(llm.solo_calls) == 4
        assert all(outcome.produced is not None for outcome in outcomes)

    def test_a_provider_that_raises_something_other_than_a_provider_error_still_falls_back(
        self,
    ):
        """A vendor payload that makes the provider raise ``KeyError`` must cost one chunk.

        Without the broad ``except`` in ``_send`` the exception escapes ``ChunkTask.run``, and
        ``run_wave`` charges it to every slot the chunk owned: K field errors where K correct
        individual calls were available.
        """

        class _BrokenProvider(_BatchingLLM):
            def generate_json(self, parts, *, schema, **kwargs):
                raise RuntimeError("vendor payload changed shape")

        llm = _BrokenProvider()
        task = ChunkTask(
            range(3),
            _works(3, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(3, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        )

        outcomes = task.run()

        assert [outcome.produced is not None for outcome in outcomes] == [True] * 3
        assert len(llm.solo_calls) == 3  # every note took its ordinary route

    def test_a_solo_thunk_that_raises_is_charged_to_its_own_note_only(self):
        """The guard in ``_solo``, driven directly — the pipeline cannot reach it.

        ``PipelineRunner`` already converts a raising TOOL into an errored result, so a test
        that raises from the provider proves the pipeline's isolation, not the chunk's. What
        this pins is the thunk itself failing (a materializer, a `make_run`): the exception
        must land in that one note's slot instead of escaping into ``run_wave``, which would
        attribute it to all K.
        """
        llm = _BatchingLLM(corrupt="drop-third")
        works = _works(5, llm)
        boom = RuntimeError("materializer exploded")
        works[2] = FieldWork(
            rule=works[2].rule,
            fields=works[2].fields,
            note_id=works[2].note_id,
            solo=lambda: (_ for _ in ()).throw(boom),
        )
        task = ChunkTask(
            range(5),
            works,
            providers=_StubHub(llm=llm),
            budget=FieldBudget(5, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        )

        outcomes = task.run()

        assert [outcome.produced is None for outcome in outcomes] == [
            False,
            False,
            True,
            False,
            False,
        ]
        assert outcomes[2].attempts[-1].error is boom

    def test_a_failing_fallback_becomes_that_notes_field_error_only(self):
        class _SoloExplodes(_BatchingLLM):
            def generate_text(self, prompt, **kwargs):
                raise ProviderError("HTTP 401")

        llm = _SoloExplodes(corrupt="drop-third")
        task = ChunkTask(
            range(5),
            _works(5, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(5, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}}"),
        )

        outcomes = task.run()

        assert [outcome.produced is None for outcome in outcomes] == [
            False,
            False,
            True,
            False,
            False,
        ]
        assert outcomes[2].errored  # kind="error", not "unproductive"


def _works(count: int, llm) -> list[FieldWork]:
    """Field work for ``count`` notes of the one-text-field config, wired to a real pipeline."""
    service = GenerationService(_StubHub(llm=llm), detect_tts_language=False)
    works: list[FieldWork] = []
    for nid in range(1, count + 1):
        run = service.make_run(_TEXT_ONLY, {"Word": f"w{nid}", "Def": ""}, note_id=nid)
        works.extend(service.works_for(run))
    return works


class TestEligibility:
    def test_a_text_ai_rule_with_a_template_is_batchable(self):
        rule = _rule(kind="text", prompt="Define {{Word}}")

        assert chunk_key(rule) == ("Vocab", "Def", "", "", "Define {{Word}}")

    @pytest.mark.parametrize("kind", ["image", "tts"])
    def test_media_rules_are_never_batchable(self, kind):
        # They return BYTES: "one call for K notes" has no meaning for a synthesis per note.
        assert chunk_key(_rule(kind=kind, prompt="say {{Word}}")) is None

    def test_a_tool_chain_is_never_batchable(self):
        # A deterministic or user-authored tool may make no provider call at all, and a chain
        # has fallbacks a single merged request cannot express.
        assert chunk_key(_rule(prompt="x {{Word}}", tools=("cloze", "ai"))) is None

    def test_a_chain_that_merely_STARTS_with_ai_is_never_batchable_either(self):
        """Pins the length check, which the case above leaves to the "first tool" check.

        ``("ai", "cloze")`` is the shape that matters: on a 429 a chunk writes an error for
        every note it owns without ever reaching the chain's later tool, where the solo path
        would have run the fallback and produced something.
        """
        assert chunk_key(_rule(prompt="x {{Word}}", tools=("ai", "cloze"))) is None

    def test_a_template_less_rule_is_never_batchable(self):
        # Its prompt IS one field's value — no shared instruction to amortise.
        assert chunk_key(_rule(prompt="")) is None

    def test_media_and_tool_fields_stay_solo_in_a_mixed_wave(self, monkeypatch):
        config = _config(
            [
                ("Def", dict(enabled=True, type="text", prompt="Define {{Word}}.")),
                (
                    "Audio",
                    dict(enabled=True, type="tts", prompt="{{Word}}", language="en"),
                ),
            ]
        )
        llm = _BatchingLLM()

        notes, _summary, _compat = _run_batch(
            monkeypatch, llm, notes=4, config=config, tts=FakeTTSProvider()
        )

        assert llm.batch_calls == [llm.batch_calls[0]]  # exactly one, for Def
        assert len(llm.batch_calls[0]) == 4
        assert all(notes[n]["Audio"].startswith("[sound:") for n in range(1, 5))

    def test_only_notes_whose_gate_said_dispatch_enter_a_chunk(self, monkeypatch):
        # Notes 2 and 4 already hold a Def and regenerate_when_batching is off, so they are
        # skipped by the gate BEFORE the wave is planned — the chunk carries the other three.
        llm = _BatchingLLM()

        _notes, _summary, _compat = _run_batch(
            monkeypatch, llm, notes=5, filled={2: "kept", 4: "kept"}
        )

        assert [len(ids) for ids in llm.batch_calls] == [3]

    def test_a_group_of_one_is_a_solo_call_not_a_one_item_chunk(self, monkeypatch):
        llm = _BatchingLLM()

        notes, _summary, _compat = _run_batch(monkeypatch, llm, notes=1)

        assert llm.batch_calls == []
        assert notes[1]["Def"] == "solo:Define w1 clearly."


def _rule(*, kind="text", prompt="Define {{Word}}", tools=("ai",)):
    from omnia.plugins.smart_notes.config import CompiledToolSpec, SmartNotesFieldRule

    return SmartNotesFieldRule(
        note_type="Vocab",
        target_field="Def",
        kind=kind,
        prompt=prompt,
        tools=tuple(CompiledToolSpec(name=name) for name in tools),
    )


class TestBatchedOutputMatchesTheSoloPath:
    def test_a_batched_result_is_byte_identical_to_the_per_note_result(self):
        """Same Markdown in, same HTML and same ``ai`` provenance stamp out.

        Without the shared tail a batched field would render differently from the identical
        field generated alone, and the summary's "fell back to a later tool" counter would start
        firing on fields that did no such thing.
        """

        class _Markdown(_BatchingLLM):
            def generate_text(self, prompt, **kwargs):
                return "**bold** and *italic*"

            def generate_json(self, parts, *, schema, **kwargs):
                items = json.loads(parts.suffix)
                return (
                    json.dumps(
                        {
                            "items": [
                                {"id": item["id"], "content": "**bold** and *italic*"}
                                for item in items
                            ]
                        }
                    ),
                    None,
                )

        llm = _Markdown()
        works = _works(2, llm)
        solo = works[0].solo()
        task = ChunkTask(
            range(2),
            works,
            providers=_StubHub(llm=llm),
            budget=FieldBudget(2, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}}"),
        )

        batched = task.run()[0]

        assert batched.produced.text == solo.produced.text
        assert batched.produced.tool == solo.produced.tool == "ai"
        assert batched.produced.kind == solo.produced.kind


class TestChunkSizing:
    def test_k_is_clamped_by_the_output_budget(self):
        # 1000 tokens of budget against the pessimistic 512-token first estimate leaves room
        # for one item, whatever the user asked for.
        budget = FieldBudget(10, output_tokens=1000)

        assert budget.size_for(("k",)) == 1

    def test_a_short_observed_answer_still_respects_the_requested_k(self):
        budget = FieldBudget(4, output_tokens=8192)
        budget.observe(("k",), 200)

        assert budget.size_for(("k",)) == 4

    def test_a_long_observed_answer_shrinks_k(self):
        budget = FieldBudget(10, output_tokens=8192)
        budget.observe(("k",), 8000)  # ~3000 tokens per item

        assert budget.size_for(("k",)) == 2

    def test_the_estimate_only_ever_grows(self):
        # A heuristic that could grow K again on one short answer would oscillate for the rest
        # of a long batch.
        budget = FieldBudget(10, output_tokens=8192)
        budget.observe(("k",), 8000)
        budget.observe(("k",), 10)

        assert budget.size_for(("k",)) == 2

    def test_the_output_cap_is_proportional_to_the_chunk_size(self):
        """A chunk asks for room per ITEM, so the same field truncates at every K or at none.

        A flat cap makes truncation a property of how many items happened to be in the chunk;
        this makes it a property of the estimate, which the budget then corrects.
        """
        budget = FieldBudget(10, output_tokens=8192)

        assert budget.tokens_for(("k",), 1) == 512  # the pessimistic first estimate
        assert budget.tokens_for(("k",), 4) == 2048
        # Never above the field's whole output ceiling, however many items are asked for.
        assert budget.tokens_for(("k",), 100) == 8192

    def test_an_unusable_answer_buys_the_retry_more_room_per_item(self):
        """Halving the chunk alone would retry with exactly the room that just failed.

        Delete the estimate growth in ``unusable`` and the half-size retry asks for 4 x 512
        again — the same tokens per item the 8-item chunk had, so a truncated field truncates
        forever instead of recovering at the second rung.
        """
        budget = FieldBudget(8, output_tokens=8192)
        assert budget.tokens_for(("k",), 8) == 4096

        budget.unusable(("k",), 4)

        assert budget.size_for(("k",)) == 4
        assert budget.tokens_for(("k",), 4) == 4096  # 4 items x 1024, not 4 x 512

    def test_a_batched_call_sends_the_cap_it_computed(self):
        """The wire assertion: the cap is explicit on every chunk, and it is this one."""
        llm = _RecordingParts()
        ChunkTask(
            range(3),
            _works(3, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(3, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        ).run()

        assert llm.max_tokens == [1536]  # 3 items x the 512-token first estimate

    def test_a_halving_is_remembered_for_the_rest_of_the_run(self, monkeypatch):
        # 10 notes, K=5: the first chunk halves and pins 2, so the remaining chunks are 2 wide.
        #
        # One worker, pinned, because a cohort is max(workers, K) and this assertion counts
        # chunks per cohort. At the shipped 8 workers the cohort is 8, so the first wave is
        # planned as 5 + 3 — and that 3 was sized BEFORE the halving was recorded, which is
        # correct behaviour and not what this test is about. Pinning the cohort to K keeps the
        # subject the budget's memory rather than the shipped worker count.
        llm = _BatchingLLM(corrupt="not-json")

        _notes, _summary, _compat = _run_batch(
            monkeypatch, llm, notes=10, notes_per_call=5, max_concurrent_generations=1
        )

        assert llm.batch_calls[0] and len(llm.batch_calls[0]) == 5
        assert max(len(ids) for ids in llm.batch_calls[3:]) <= 2


class TestPlanning:
    def test_the_solo_planner_makes_one_task_per_field(self):
        llm = _BatchingLLM()
        works = _works(3, llm)

        tasks = SOLO_PLANNER.plan(works)

        assert [task.slots for task in tasks] == [(0,), (1,), (2,)]
        assert all(isinstance(task, SoloTask) for task in tasks)

    def test_the_runner_covers_every_slot_exactly_once(self):
        llm = _BatchingLLM()
        works = _works(7, llm)

        tasks = FieldBatchRunner(_StubHub(llm=llm), notes_per_call=3).plan(works)

        covered = [slot for task in tasks for slot in task.slots]
        assert sorted(covered) == list(range(7))
        assert len(covered) == len(set(covered))

    def test_tasks_come_back_in_wave_order(self):
        llm = _BatchingLLM()
        tasks = FieldBatchRunner(_StubHub(llm=llm), notes_per_call=3).plan(
            _works(7, llm)
        )

        assert [task.slots[0] for task in tasks] == sorted(
            task.slots[0] for task in tasks
        )


class TestTheShippedDefaultGroups:
    def test_the_shipped_default_answers_five_notes_in_one_call(self, monkeypatch):
        """K = 10 out of the box, so five notes of one field cost ONE request, not five.

        This is the whole point of the default and the only thing measured that supports it:
        requests, not seconds. At the shipped worker count of 1 there is no parallelism for the
        chunk to serialise, so the request saving is free.
        """
        llm = _BatchingLLM()

        notes, _summary, _compat = _run_batch(
            monkeypatch,
            llm,
            notes=5,
            notes_per_call=SmartNotesSettings().batch_notes_per_call,
        )

        assert len(llm.batch_calls) == 1 and len(llm.batch_calls[0]) == 5
        assert llm.solo_calls == []
        assert notes[1]["Def"] == "batched:w1"

    def test_the_planner_for_k_of_one_is_the_solo_planner_itself(self):
        service = GenerationService(_StubHub(llm=_BatchingLLM()))

        assert service.batch_planner(notes_per_call=1) is SOLO_PLANNER


class TestPromptEnvelope:
    def test_the_task_is_quoted_verbatim_and_uninterpolated(self):
        llm = _RecordingParts()
        task = ChunkTask(
            range(2),
            _works(2, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(2, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        )

        task.run()

        assert "Define {{Word}} clearly." in llm.parts[0].prefix
        assert "w1" not in llm.parts[0].prefix  # values never reach the cacheable head

    def test_the_prefix_is_identical_across_chunks_of_the_same_field(self):
        # This is what makes LAYER 2 and LAYER 3 compose instead of compete.
        llm = _RecordingParts()
        works = _works(4, llm)
        for start in (0, 2):
            ChunkTask(
                range(2),
                works[start : start + 2],
                providers=_StubHub(llm=llm),
                budget=FieldBudget(2, output_tokens=8192),
                key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
            ).run()

        assert llm.parts[0].prefix == llm.parts[1].prefix
        assert llm.parts[0].suffix != llm.parts[1].suffix

    def test_an_item_carries_only_the_values_its_own_template_names(self):
        llm = _RecordingParts()
        ChunkTask(
            range(2),
            _works(2, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(2, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        ).run()

        items = json.loads(llm.parts[0].suffix)
        assert [set(item["values"]) for item in items] == [{"Word"}, {"Word"}]

    def test_a_schema_is_offered_to_the_provider(self):
        llm = _RecordingParts()
        ChunkTask(
            range(2),
            _works(2, llm),
            providers=_StubHub(llm=llm),
            budget=FieldBudget(2, output_tokens=8192),
            key=("Vocab", "Def", "", "", "Define {{Word}} clearly."),
        ).run()

        assert llm.schemas[0]["properties"]["items"]["type"] == "array"


class _RecordingParts(_BatchingLLM):
    """Captures the ``PromptParts``, schema and output cap each batched call was built with."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[PromptParts] = []
        self.schemas: list[dict] = []
        self.max_tokens: list[int | None] = []

    def generate_json(self, parts, *, schema, **kwargs):
        self.parts.append(parts)
        self.schemas.append(schema)
        self.max_tokens.append(kwargs.get("max_tokens"))
        return super().generate_json(parts, schema=schema, **kwargs)


class TestConcurrentBatching:
    def test_batching_and_pooling_together_still_route_by_id(self, monkeypatch):
        """Several chunks in flight on several threads, one deliberately short response.

        The two layers meet here: the pool makes completion order disagree with wave order, and
        the short response makes the id map the only thing that can place the answers.
        """
        llm = _BatchingLLM(corrupt="drop-third")

        notes, summary, _compat = _run_batch(
            monkeypatch,
            llm,
            notes=12,
            notes_per_call=4,
            max_concurrent_generations=4,
        )

        for nid in range(1, 13):
            assert notes[nid]["Def"] in (
                f"batched:w{nid}",
                f"solo:Define w{nid} clearly.",
            )
        assert summary.processed == 12
        assert len(llm.solo_calls) == 3  # one dropped item per chunk of four


class _CannedTask(WaveTask):
    """A task that answers with a fixed list — or raises. The scatter needs nothing more."""

    def __init__(self, slots, outcomes=None, error=None) -> None:
        super().__init__(slots)
        self._outcomes = outcomes or []
        self._error = error

    def run(self):
        if self._error is not None:
            raise self._error
        return list(self._outcomes)


class TestWaveScatter:
    """``run_wave`` is the only place a task's outcomes are paired back onto notes."""

    def test_outcomes_come_back_in_wave_order_whatever_the_task_order(self):
        # Tasks deliberately out of slot order, and one covering three slots out of order too:
        # the wave is addressed by SLOT, never by position in the dispatch list.
        tasks = [
            _CannedTask((3,), ["d"]),
            _CannedTask((0, 2, 1), ["a", "c", "b"]),
        ]

        assert run_wave(tasks, 4, SEQUENTIAL_DISPATCH) == ["a", "b", "c", "d"]

    def test_a_task_that_raises_is_attributed_to_its_own_slots_only(self):
        # Escaping instead would reach the batch's on_failure, which reports the WHOLE selection
        # as failed.
        tasks = [
            _CannedTask((0,), error=RuntimeError("task exploded")),
            _CannedTask((1,), ["ok"]),
        ]

        outcomes = run_wave(tasks, 2, SEQUENTIAL_DISPATCH)

        assert isinstance(outcomes[0], RuntimeError)
        assert outcomes[1] == "ok"

    def test_a_task_answering_for_the_wrong_number_of_slots_errors_every_one_of_them(
        self,
    ):
        # A bug, but one that must surface as explicit field errors rather than as content
        # silently landing on the wrong note.
        outcomes = run_wave([_CannedTask((0, 1), ["only-one"])], 2, SEQUENTIAL_DISPATCH)

        assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)

    def test_every_slot_is_filled(self):
        """A hole would reach ``NoteRun.commit`` as neither a result nor an exception."""
        llm = _BatchingLLM()
        works = _works(5, llm)
        tasks = FieldBatchRunner(_StubHub(llm=llm), notes_per_call=2).plan(works)

        outcomes = run_wave(tasks, len(works), SEQUENTIAL_DISPATCH)

        assert len(outcomes) == 5
        assert all(outcome is not None for outcome in outcomes)
