"""Tests for LAYER 1: per-level and cross-note concurrency in smart-notes generation.

The invariant under test throughout is that concurrency changes only the WALL CLOCK. Same
results, same order, same blocked/failed bookkeeping, same media, same progress, same cancel
semantics — whether a level runs one field at a time in this thread or four fields at once on
a pool.

The provider fakes here deliberately return LATER rules FIRST (inverted latency): a runner that
assembles its output in completion order rather than input order passes every same-speed test
and fails these.
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider

from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH
from omnia.core.concurrency.pool import (
    PooledDispatch,
    pooled_dispatch,
    request_capacity,
)
from omnia.core.network.http import HttpClient, ThrottledHttpClient
from omnia.core.network.limiter import PROVIDER_LIMITER
from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration.batch import BatchGenerator


class _CountingTransport(HttpClient):
    """Counts overlapping requests, holding each until ``expect`` of them are in flight.

    It used to hold each request for a fixed 20 ms and hope that was long enough for all of them
    to overlap. That makes the observed peak a race against thread start-up: on a loaded machine
    the eighth worker can arrive after the first has finished, and the control assertion
    ("unthrottled observes 8") fails for a reason that has nothing to do with the limiter — a
    flake in the one test that proves the bound binds. Waiting for the expected overlap instead,
    with a deadline so a bound that legitimately prevents it still returns, makes the peak a
    property of the bound rather than of the scheduler.
    """

    def __init__(
        self, expect: int, *, grace: float = 0.02, timeout: float = 5.0
    ) -> None:
        self._expect = expect
        self._grace = grace
        self._timeout = timeout
        self._condition = threading.Condition()
        self._in_flight = 0
        self._reached = False
        self.peak = 0

    def _send(self):
        deadline = time.monotonic() + self._timeout
        with self._condition:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            if self._in_flight >= self._expect:
                # Latched, not re-tested: the first waiter to wake decrements, and a plain
                # ``in_flight >= expect`` check would send its siblings back to sleep.
                self._reached = True
            self._condition.notify_all()
            while not self._reached:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break  # the bound really does prevent this many — that is the finding
                self._condition.wait(remaining)
        # Then held a little longer, WITHOUT the lock. The wait above guarantees the peak reaches
        # ``expect``; this is what lets it EXCEED it. Without the grace window a rig told to
        # expect 3 would release the moment 3 arrived and report 3 even with the bound removed —
        # it would measure its own parameter instead of the limiter.
        time.sleep(self._grace)
        with self._condition:
            self._in_flight -= 1
        return {}

    def post_json(self, url, payload, *, headers=None):
        return self._send()

    def post_form(self, url, fields, *, headers=None):
        return self._send()

    def post_json_for_bytes(self, url, payload, *, headers=None):
        self._send()
        return b""

    def get_bytes(self, url, *, params=None, headers=None):
        self._send()
        return b""

    def get_json(self, url, *, params=None, headers=None):
        return self._send()


def _peak_concurrent_requests(*, workers: int, expect: int) -> int:
    """Run ``workers`` request-making units through the real pool + real ThrottledHttpClient.

    Deliberately the production objects: ``pooled_dispatch`` sets the capacity,
    ``ThrottledHttpClient`` spends the permit. A hand-rolled rig would prove the limiter class
    works (``tests/core/test_provider_limiter.py`` already does) without proving that anything
    in the generation path is wired to it, which was exactly the gap.

    ``expect`` is how many requests SHOULD manage to overlap — the bound under test, or the
    worker count when nothing bounds them. It only controls how long each request is held (see
    :class:`_CountingTransport`); a limiter that let more through still reports the higher peak,
    so the assertion is not weakened by telling the rig what it is looking for.
    """
    transport = _CountingTransport(expect)
    client = ThrottledHttpClient(transport, PROVIDER_LIMITER)
    with pooled_dispatch(workers) as dispatch:
        dispatch.run([lambda: client.post_json("https://x", {})] * workers)
    return transport.peak


class _InvertedLatencyLLM(FakeLLMProvider):
    """Answers a LATER field sooner than an earlier one, and records the calling thread.

    The point is to make completion order disagree with input order on purpose: assembling
    results by completion is the bug this fake exists to catch.
    """

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []
        self._lock = threading.Lock()

    def generate_text(self, prompt, *, system=None, temperature=None, max_tokens=None):
        with self._lock:
            self.threads.append(threading.get_ident())
        # "zzz" prompts finish immediately, "aaa" prompts dawdle.
        time.sleep(0.001 if prompt.startswith("z") else 0.03)
        return f"gen:{prompt}"

    def generate_image(self, prompt, *, size="1024x1024"):
        return f"IMG<{prompt}>".encode()


class _EchoTTS(FakeTTSProvider):
    def synthesize(self, text, *, lang=None, voice=None):
        return f"AUDIO<{text}>".encode()


class _StubHub:
    def __init__(self, *, llm=None, tts=None, auto_voices=None) -> None:
        self._llm = llm
        self._tts = tts
        self._auto_voices = auto_voices or {"en": ("fake", "en-voice")}

    def llm(self, *, model: str = "", image_model: str = "", provider: str = ""):
        return self._llm

    def tts(self, *, provider: str = ""):
        return self._tts

    def resolve_auto_voice(self, lang: str, *, reason: str = ""):
        if lang not in self._auto_voices:
            raise ProviderError(f"No Auto-detect voice set for language {lang!r}")
        return self._auto_voices[lang]


def _config(fields):
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field="Word",
        fields=[SmartNotesFieldConfig(field=name, **kw) for name, kw in fields],
    )


def _multilevel_config():
    """Four independent level-0 fields (inverted latency) + one dependent on the slowest."""
    return _config(
        [
            ("A", dict(enabled=True, type="text", prompt="aaa {{Word}}")),
            ("B", dict(enabled=True, type="text", prompt="zzz {{Word}}")),
            ("C", dict(enabled=True, type="text", prompt="zzz2 {{Word}}")),
            ("D", dict(enabled=True, type="text", prompt="aaa2 {{Word}}")),
            ("E", dict(enabled=True, type="text", prompt="zzz3 {{A}}")),
        ]
    )


def _fields():
    return {"Word": "cat", "A": "", "B": "", "C": "", "D": "", "E": ""}


class TestParallelMatchesSequential:
    def test_parallel_dispatch_returns_the_same_triple_as_sequential(self):
        config = _multilevel_config()
        sequential = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))
        parallel = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))

        expected = sequential.generate_note(config, _fields())
        pool = PooledDispatch(4)
        try:
            actual = parallel.generate_note(config, _fields(), dispatch=pool)
        finally:
            pool.close()

        assert [rule.target_field for rule, _ in actual[0]] == [
            rule.target_field for rule, _ in expected[0]
        ]
        assert [result.text for _, result in actual[0]] == [
            result.text for _, result in expected[0]
        ]
        assert actual[1] == expected[1]
        assert actual[2] == expected[2]

    def test_a_level_really_runs_on_several_threads(self):
        llm = _InvertedLatencyLLM()
        service = GenerationService(_StubHub(llm=llm))
        pool = PooledDispatch(4)
        try:
            service.generate_note(_multilevel_config(), _fields(), dispatch=pool)
        finally:
            pool.close()

        assert len(set(llm.threads)) > 1

    def test_the_dependent_field_still_sees_its_prerequisite(self):
        # E's prompt references A, which is in an earlier level; parallelism inside a level
        # must not let a level start before the previous one has committed.
        service = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))
        pool = PooledDispatch(4)
        try:
            results, _blocked, _failed = service.generate_note(
                _multilevel_config(), _fields(), dispatch=pool
            )
        finally:
            pool.close()

        produced = {rule.target_field: result.text for rule, result in results}
        assert produced["E"] == "gen:zzz3 gen:aaa cat"


class TestSnapshotIsolation:
    def test_the_working_snapshot_is_read_only_inside_a_level(self):
        """A tool that mutates what it was handed must fail loudly, not change a sibling."""
        service = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))
        run = service.make_run(_multilevel_config(), _fields())
        run.next_dispatch()

        with pytest.raises(TypeError):
            run.snapshot["Word"] = "dog"  # type: ignore[index]

    def test_the_callers_field_map_is_never_mutated(self):
        service = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))
        fields = _fields()
        pool = PooledDispatch(4)
        try:
            service.generate_note(_multilevel_config(), fields, dispatch=pool)
        finally:
            pool.close()

        assert fields == _fields()


class TestFailureAttribution:
    def test_a_worker_exception_becomes_that_field_and_no_other(self):
        """A unit that raises outside the pipeline's own guard must not escape the wave.

        Escaping would reach the batch's ``on_failure``, which reports the WHOLE selection as
        failed — 49 successful notes reported as failures, and their clips reconsidered for
        discard.
        """

        class _Exploding:
            def run(self, units):
                return [
                    RuntimeError("pool exploded") if index == 0 else unit()
                    for index, unit in enumerate(units)
                ]

        service = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))
        results, blocked, failed = service.generate_note(
            _multilevel_config(), _fields(), dispatch=_Exploding()
        )

        assert [item.field for item in failed] == ["A"]
        assert failed[0].error == "pool exploded"
        assert failed[0].kind == "error"  # keeps the note out of empty_note_ids
        # …and E blocks transitively on the field that broke, while its siblings still ran.
        assert [item.target_field for item in blocked] == ["E"]
        assert [rule.target_field for rule, _ in results] == ["B", "C", "D"]

    def test_a_failed_field_carries_its_note_id(self):
        service = GenerationService(_StubHub(llm=_InvertedLatencyLLM()))

        class _Exploding:
            def run(self, units):
                return [RuntimeError("boom") for _ in units]

        _results, _blocked, failed = service.generate_note(
            _config([("A", dict(enabled=True, type="text", prompt="a {{Word}}"))]),
            {"Word": "cat", "A": ""},
            note_id=4242,
            dispatch=_Exploding(),
        )

        assert failed[0].note_id == 4242


class TestPoolLifetime:
    def test_pooled_dispatch_shuts_the_pool_down_on_exit(self):
        with pooled_dispatch(3) as dispatch:
            assert isinstance(dispatch, PooledDispatch)
            captured = dispatch

        with pytest.raises(RuntimeError):
            captured.run([lambda: 1])

    def test_pooled_dispatch_shuts_the_pool_down_when_the_body_raises(self):
        captured = {}
        with pytest.raises(ValueError):
            with pooled_dispatch(3) as dispatch:
                captured["pool"] = dispatch
                raise ValueError("boom")

        with pytest.raises(RuntimeError):
            captured["pool"].run([lambda: 1])

    def test_one_worker_starts_no_threads_at_all(self):
        # The conservative default must COST nothing, not merely behave as if it did.
        with pooled_dispatch(1) as dispatch:
            assert dispatch is SEQUENTIAL_DISPATCH

    def test_the_limiter_is_narrowed_to_the_workers_and_restored_afterwards(self):
        """No ``+1``: a capacity one above the most permits anyone can hold never binds.

        The restore is the other half. Capacity is process-wide state, so a run that left its
        own number behind would make the bound depend on which generation happened to run last
        in the session — including after a run at the ceiling, which would leave the limiter
        wide open for every later interactive call.
        """
        from omnia.core.network.limiter import PROVIDER_LIMITER

        resting = PROVIDER_LIMITER.capacity
        with pooled_dispatch(4):
            assert PROVIDER_LIMITER.capacity == 4
        assert PROVIDER_LIMITER.capacity == resting

    def test_a_sequential_run_still_narrows_the_provider_bound(self):
        """One unit at a time is also one REQUEST at a time — including for a call arriving
        from elsewhere mid-run, which the old ``+1`` would have waved through."""
        from omnia.core.network.limiter import PROVIDER_LIMITER

        resting = PROVIDER_LIMITER.capacity
        with pooled_dispatch(1):
            assert PROVIDER_LIMITER.capacity == 1
        assert PROVIDER_LIMITER.capacity == resting

    def test_the_request_bound_can_be_set_below_the_worker_count(self, monkeypatch):
        """The case a bound derived from the pool width cannot express, and the reason the
        limiter is a separate mechanism: 8 units at once, 3 requests in flight."""
        monkeypatch.setenv("OMNIA_MAX_CONCURRENT_REQUESTS", "3")

        assert request_capacity(8) == 3

    def test_the_limiter_actually_blocks_a_pool_wider_than_the_bound(self, monkeypatch):
        """Peak concurrent REQUESTS never exceeds the capacity, under a load that would.

        This is the test that fails if the limiter stops limiting. The workload is eight pool
        workers each making a request that overlaps the others by construction (every one sleeps
        inside the transport), so unthrottled the transport would observe a peak of 8; with the
        bound at 3 it must observe at most 3. The unthrottled control run is asserted too —
        without it, a limiter that blocked EVERYTHING would pass just as well.
        """
        monkeypatch.setenv("OMNIA_MAX_CONCURRENT_REQUESTS", "3")

        throttled = _peak_concurrent_requests(workers=8, expect=3)

        monkeypatch.delenv("OMNIA_MAX_CONCURRENT_REQUESTS")
        monkeypatch.setattr(
            "omnia.core.concurrency.pool.request_capacity",
            lambda workers: 999,
        )
        unthrottled = _peak_concurrent_requests(workers=8, expect=8)

        assert (
            unthrottled == 8
        ), "the workload must exceed the bound when nothing limits it"
        assert throttled == 3


class _FakeCard:
    def __init__(self, did: int) -> None:
        self.did = did


class _FakeNote:
    def __init__(self, nid, note_type, fields, decks=(1,)) -> None:
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)
        self._decks = list(decks)

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
        return [_FakeCard(did) for did in self._decks]


class _RecordingCompat:
    """Records note writes, media writes (with their thread) and progress values."""

    def __init__(self, notes) -> None:
        self._notes = notes
        self.updated: list[int] = []
        self.media_threads: list[int] = []
        self.progress_values: list[int] = []

    def get_note(self, nid, col=None):
        return self._notes[nid]

    def note_deck_ids(self, note, col=None):
        return [int(c.did) for c in note.cards()]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def add_media_file(self, filename, data, col=None):
        self.media_threads.append(threading.get_ident())
        return filename

    def progress_start(self, label, maximum):
        self.progress_values.append(0)

    def progress_update(self, label, value, maximum):
        self.progress_values.append(value)

    def progress_finish(self):
        pass

    def progress_was_cancelled(self):
        return False

    def run_on_main(self, callback):
        callback()

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:
            if on_failure:
                on_failure(exc)


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


class TestCrossNoteOverlap:
    def _settings(self, workers):
        return SmartNotesSettings(
            note_types=[
                _config(
                    [
                        ("A", dict(enabled=True, type="text", prompt="aaa {{Word}}")),
                        (
                            "Audio",
                            dict(
                                enabled=True,
                                type="tts",
                                prompt="say {{Word}}",
                                language="en",
                            ),
                        ),
                    ]
                )
            ],
            regenerate_when_batching=False,
            max_concurrent_generations=workers,
        )

    def _run(self, monkeypatch, workers, count=6):
        notes = {
            n: _FakeNote(n, "Basic", {"Word": f"w{n}", "A": "", "Audio": ""})
            for n in range(1, count + 1)
        }
        fake = _RecordingCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(workers)
        service = GenerationService(
            _StubHub(llm=_InvertedLatencyLLM(), tts=_EchoTTS()),
            detect_tts_language=False,
        )
        summaries: list = []
        BatchGenerator(service, settings).run(
            list(range(1, count + 1)), summaries.append
        )
        return fake, summaries[0], notes

    def test_every_note_is_generated_and_written_in_selection_order(self, monkeypatch):
        fake, summary, notes = self._run(monkeypatch, workers=3)

        assert summary.processed == 6
        assert fake.updated == [1, 2, 3, 4, 5, 6]
        assert notes[4]["A"] == "gen:aaa w4"

    def test_concurrency_does_not_change_the_outcome(self, monkeypatch):
        _fake_one, sequential, notes_one = self._run(monkeypatch, workers=1)
        _fake_many, parallel, notes_many = self._run(monkeypatch, workers=4)

        assert sequential.message() == parallel.message()
        assert {n: dict(note._fields) for n, note in notes_one.items()} == {
            n: dict(note._fields) for n, note in notes_many.items()
        }

    def test_materialize_is_called_only_from_the_driver_thread(self, monkeypatch):
        """Media must never be written from a pool worker.

        It is what makes the per-note materializer memo safe without a lock, and it is why a
        cancelled wave cannot leave an orphaned file behind. One distinct thread id across a
        media-bearing multi-note batch is the whole assertion.
        """
        fake, _summary, _notes = self._run(monkeypatch, workers=4)

        assert len(fake.media_threads) == 6
        assert len(set(fake.media_threads)) == 1

    def test_progress_is_monotonic_and_never_exceeds_the_total(self, monkeypatch):
        fake, _summary, _notes = self._run(monkeypatch, workers=3)

        assert fake.progress_values == sorted(fake.progress_values)
        assert fake.progress_values[-1] == 6
