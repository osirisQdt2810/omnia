"""Self-tracked usage: the JSON recorder, the recording provider wrappers, and hub wiring."""

from __future__ import annotations

import os

import pytest
from conftest import FakeHttpClient, FakeLLMProvider, FakeTTSProvider

from omnia.core.config.models import (
    LLMSettings,
    OpenAICompatibleLLMSettings,
    TTSSettings,
)
from omnia.core.providers import ProviderHub
from omnia.core.providers.usage import (
    BufferedUsageRecorder,
    ColUsageStore,
    JsonUsageRecorder,
    JsonUsageStore,
    NullUsageRecorder,
    RecordingLLMProvider,
    RecordingTTSProvider,
    _fold_call,
    default_recorder,
    flush_default_recorder,
    set_default_recorder,
)


class _FakeRecorder:
    """A recorder that captures the kwargs of every record() call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _RaisingRecorder:
    """A recorder that always raises — to prove recording errors are swallowed."""

    def record(self, **kwargs) -> None:
        raise RuntimeError("boom")


class _UsageLLM(FakeLLMProvider):
    """A fake LLM that reports exact token usage like a real provider response."""

    def __init__(self) -> None:
        super().__init__(text="hi")
        self.last_usage = {"in": 42, "out": 8, "total": 50}


class TestRecordsRealTokens:
    def test_wrapper_records_provider_reported_tokens(self):
        rec = _FakeRecorder()
        RecordingLLMProvider(_UsageLLM(), rec, model="m").generate_text("prompt")
        call = rec.calls[0]
        assert call["in_tokens"] == 42 and call["out_tokens"] == 8

    def test_wrapper_records_zero_tokens_when_unreported(self):
        rec = _FakeRecorder()
        RecordingLLMProvider(FakeLLMProvider(), rec, model="m").generate_text("p")
        call = rec.calls[0]
        assert call["in_tokens"] == 0 and call["out_tokens"] == 0
        assert call["in_chars"] == 1  # char fallback still recorded


class TestJsonUsageRecorder:
    """The recorder now DELEGATES persistence to an injected :class:`JsonUsageStore`."""

    def test_round_trip_aggregates_calls_and_chars(self, tmp_path):
        clock = {"t": 100.0}
        recorder = JsonUsageRecorder(
            JsonUsageStore(tmp_path / "usage.json"), time_fn=lambda: clock["t"]
        )
        recorder.record(
            kind="text", provider="gemini", model="m", in_chars=10, out_chars=5
        )
        clock["t"] = 200.0
        recorder.record(
            kind="text", provider="gemini", model="m", in_chars=3, out_chars=7
        )
        rows = recorder.snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["calls"] == 2
        assert row["in_chars"] == 13
        assert row["out_chars"] == 12
        assert row["last_used_ts"] == 200.0

    def test_aggregates_in_and_out_tokens(self, tmp_path):
        recorder = JsonUsageRecorder(JsonUsageStore(tmp_path / "usage.json"))
        recorder.record(
            kind="text",
            provider="gemini",
            model="m",
            in_chars=1,
            out_chars=1,
            in_tokens=40,
            out_tokens=9,
        )
        recorder.record(
            kind="text",
            provider="gemini",
            model="m",
            in_chars=1,
            out_chars=1,
            in_tokens=2,
            out_tokens=3,
        )
        row = recorder.snapshot()[0]
        assert row["in_tokens"] == 42 and row["out_tokens"] == 12

    def test_distinct_keys_are_separate_rows(self, tmp_path):
        recorder = JsonUsageRecorder(JsonUsageStore(tmp_path / "usage.json"))
        recorder.record(
            kind="text", provider="gemini", model="a", in_chars=1, out_chars=1
        )
        recorder.record(
            kind="sound", provider="gemini", model="a", in_chars=1, out_chars=1
        )
        recorder.record(
            kind="text", provider="openai", model="a", in_chars=1, out_chars=1
        )
        assert len(recorder.snapshot()) == 3

    def test_snapshot_on_missing_file_is_empty(self, tmp_path):
        recorder = JsonUsageRecorder(JsonUsageStore(tmp_path / "nope.json"))
        assert recorder.snapshot() == []

    def test_snapshot_on_corrupt_file_is_empty(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text("not json{", encoding="utf-8")
        assert JsonUsageRecorder(JsonUsageStore(path)).snapshot() == []

    def test_dump_is_atomic_via_temp_then_replace(self, tmp_path, monkeypatch):
        # Regression (L3): the write (now the store's) must go through a temp file + os.replace so
        # a mid-write failure can't truncate the existing usage.json to nothing. Make os.replace
        # raise, then confirm the prior content survives intact (only the temp file was touched).
        path = tmp_path / "usage.json"
        recorder = JsonUsageRecorder(JsonUsageStore(path))
        recorder.record(
            kind="text", provider="gemini", model="m", in_chars=1, out_chars=1
        )
        before = path.read_text(encoding="utf-8")

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            recorder.record(
                kind="text", provider="gemini", model="m", in_chars=1, out_chars=1
            )
        assert path.read_text(encoding="utf-8") == before


class TestRecordingLLMProvider:
    def test_records_text_in_and_out_chars(self):
        recorder = _FakeRecorder()
        provider = RecordingLLMProvider(
            FakeLLMProvider(text="hello"), recorder, model="m1"
        )
        result = provider.generate_text("prompt", system="sys")
        assert result == "hello"
        call = recorder.calls[0]
        assert call["kind"] == "text"
        assert call["provider"] == "fake"
        assert call["model"] == "m1"
        assert call["in_chars"] == len("prompt") + len("sys")
        assert call["out_chars"] == len("hello")

    def test_records_image_in_and_out_bytes(self):
        recorder = _FakeRecorder()
        provider = RecordingLLMProvider(
            FakeLLMProvider(image=b"PNGDATA"), recorder, model="img"
        )
        data = provider.generate_image("a cat")
        assert data == b"PNGDATA"
        call = recorder.calls[0]
        assert call["kind"] == "image"
        assert call["in_chars"] == len("a cat")
        assert call["out_chars"] == len(b"PNGDATA")

    def test_image_recorded_under_image_model_not_text_model(self):
        # Regression: an image call must be logged under the IMAGE model, never the text model —
        # otherwise the Account → Image usage table shows a text-model row (e.g. gemini-3.5-flash
        # appearing under Image). Text and image are distinct models on the same provider.
        recorder = _FakeRecorder()
        provider = RecordingLLMProvider(
            FakeLLMProvider(),
            recorder,
            model="gemini-3.5-flash",
            image_model="gemini-2.5-flash-image",
        )
        provider.generate_text("hi")
        provider.generate_image("a cat")
        text_call = next(c for c in recorder.calls if c["kind"] == "text")
        image_call = next(c for c in recorder.calls if c["kind"] == "image")
        assert text_call["model"] == "gemini-3.5-flash"
        assert image_call["model"] == "gemini-2.5-flash-image"

    def test_image_falls_back_to_text_model_when_no_image_model(self):
        recorder = _FakeRecorder()
        provider = RecordingLLMProvider(FakeLLMProvider(), recorder, model="only-model")
        provider.generate_image("a cat")
        assert recorder.calls[0]["model"] == "only-model"

    def test_blank_model_records_default_label(self):
        recorder = _FakeRecorder()
        provider = RecordingLLMProvider(FakeLLMProvider(), recorder, model="")
        provider.generate_text("p")
        assert recorder.calls[0]["model"] == "(default)"

    def test_proxies_name_and_requires_api(self):
        provider = RecordingLLMProvider(
            FakeLLMProvider(), NullUsageRecorder(), model="m"
        )
        assert provider.name == "fake"
        assert provider.requires_api is True

    def test_recording_error_does_not_break_generate(self):
        provider = RecordingLLMProvider(
            FakeLLMProvider(text="ok"), _RaisingRecorder(), model="m"
        )
        assert provider.generate_text("p") == "ok"


class TestRecordingTTSProvider:
    def test_records_sound_with_voice_as_model(self):
        recorder = _FakeRecorder()
        provider = RecordingTTSProvider(FakeTTSProvider(audio=b"audio-bytes"), recorder)
        audio = provider.synthesize("speak this", voice="alloy")
        assert audio == b"audio-bytes"
        call = recorder.calls[0]
        assert call["kind"] == "sound"
        assert call["provider"] == "fake"
        assert call["model"] == "alloy"
        assert call["in_chars"] == len("speak this")
        assert call["out_chars"] == len(b"audio-bytes")

    def test_no_voice_records_default_label(self):
        recorder = _FakeRecorder()
        provider = RecordingTTSProvider(FakeTTSProvider(), recorder)
        provider.synthesize("hi")
        assert recorder.calls[0]["model"] == "(default)"

    def test_proxies_audio_ext_and_name(self):
        provider = RecordingTTSProvider(FakeTTSProvider(), NullUsageRecorder())
        assert provider.audio_ext == "mp3"
        assert provider.name == "fake"

    def test_recording_error_does_not_break_synthesize(self):
        provider = RecordingTTSProvider(
            FakeTTSProvider(audio=b"a" * 10), _RaisingRecorder()
        )
        assert provider.synthesize("hi") == b"a" * 10


class TestProviderHubRecording:
    def _llm_settings(self) -> LLMSettings:
        return LLMSettings(
            provider="openai",
            openai=OpenAICompatibleLLMSettings(
                api_key="k",
                text_model="active-model",
                image_model="active-image-model",
            ),
        )

    def test_image_call_records_under_image_model(self):
        # The hub must wire the configured image_model into the recording wrapper so an image
        # generation is logged under it, not under the text model.
        import base64

        recorder = _FakeRecorder()
        png = b"\x89PNG-bytes"
        hub = ProviderHub(
            self._llm_settings(),
            http=FakeHttpClient(
                json={"data": [{"b64_json": base64.b64encode(png).decode()}]}
            ),
            recorder=recorder,
        )
        hub.llm().generate_image("a cat")
        call = recorder.calls[0]
        assert call["kind"] == "image"
        assert call["model"] == "active-image-model"

    def test_llm_call_records_under_resolved_model(self):
        recorder = _FakeRecorder()
        hub = ProviderHub(
            self._llm_settings(),
            http=FakeHttpClient(
                json={"choices": [{"message": {"content": "hi there"}}]}
            ),
            recorder=recorder,
        )
        result = hub.llm().generate_text("prompt")
        assert result == "hi there"
        call = recorder.calls[0]
        assert call["kind"] == "text"
        assert call["model"] == "active-model"
        assert call["out_chars"] == len("hi there")

    def test_llm_override_records_under_override_model(self):
        recorder = _FakeRecorder()
        hub = ProviderHub(
            self._llm_settings(),
            http=FakeHttpClient(json={"choices": [{"message": {"content": "ok"}}]}),
            recorder=recorder,
        )
        hub.llm(model="rule-model").generate_text("p")
        assert recorder.calls[0]["model"] == "rule-model"

    def test_tts_call_records_sound(self):
        recorder = _FakeRecorder()
        hub = ProviderHub(
            self._llm_settings(),
            TTSSettings(provider="google_translate"),
            http=FakeHttpClient(data=b"ID3" + b"\x00" * 600),
            recorder=recorder,
        )
        hub.tts().synthesize("hello", lang="en")
        call = recorder.calls[0]
        assert call["kind"] == "sound"
        assert call["provider"] == "google_translate"


class _FakeUsageStore:
    """A UsageStore that records every save and can seed an initial aggregate."""

    def __init__(self, initial: dict | None = None) -> None:
        self.saves: list[dict] = []
        self._data = dict(initial or {})

    def load(self) -> dict:
        return dict(self._data)

    def save(self, data: dict) -> None:
        self.saves.append(data)
        self._data = data


def _inline(cb):
    """A synchronous ``schedule_main`` that runs the flush immediately (main-thread stand-in)."""
    cb()


class TestJsonUsageStore:
    def test_round_trips_rows(self, tmp_path):
        store = JsonUsageStore(tmp_path / "usage.json")
        store.save({"k": {"calls": 3}})
        assert JsonUsageStore(tmp_path / "usage.json").load() == {"k": {"calls": 3}}

    def test_missing_and_corrupt_load_empty(self, tmp_path):
        assert JsonUsageStore(tmp_path / "nope.json").load() == {}
        path = tmp_path / "usage.json"
        path.write_text("not json{", encoding="utf-8")
        assert JsonUsageStore(path).load() == {}


class _SqliteDb:
    """Expose an in-memory ``sqlite3`` connection through Anki's ``.execute``/``.scalar``."""

    def __init__(self) -> None:
        import sqlite3

        self._conn = sqlite3.connect(":memory:")

    def execute(self, sql: str, *args: object):
        return self._conn.execute(sql, args).fetchall()

    def scalar(self, sql: str, *args: object):
        row = self._conn.execute(sql, args).fetchone()
        return row[0] if row else None


def _aggregate_row(**over) -> dict:
    """A full aggregate row with the nine persisted fields, overridable per test."""
    row = {
        "kind": "text",
        "provider": "gemini",
        "model": "m",
        "calls": 3,
        "in_chars": 30,
        "out_chars": 12,
        "in_tokens": 40,
        "out_tokens": 9,
        "last_used_ts": 123.5,
    }
    row.update(over)
    return row


class TestColUsageStore:
    def test_round_trips_the_full_aggregate(self):
        db = _SqliteDb()
        store = ColUsageStore(db_provider=lambda: db)
        data = {"text|gemini|m": _aggregate_row()}
        store.save(data)
        # A fresh store over the SAME db reads back the identical aggregate (all nine fields).
        assert ColUsageStore(db_provider=lambda: db).load() == data

    def test_ensure_is_idempotent(self):
        db = _SqliteDb()
        store = ColUsageStore(db_provider=lambda: db)
        store.ensure()
        store.ensure()  # second call must not raise (CREATE TABLE IF NOT EXISTS)
        assert store.load() == {}

    def test_save_replaces_all_rows(self):
        db = _SqliteDb()
        store = ColUsageStore(db_provider=lambda: db)
        store.save({"text|g|a": _aggregate_row(provider="g", model="a")})
        store.save({"sound|g|b": _aggregate_row(kind="sound", provider="g", model="b")})
        # Replace-all semantics: only the second aggregate survives.
        assert list(store.load().keys()) == ["sound|g|b"]

    def test_preserves_in_and_out_tokens(self):
        db = _SqliteDb()
        store = ColUsageStore(db_provider=lambda: db)
        store.save({"text|g|m": _aggregate_row(in_tokens=42, out_tokens=8)})
        row = store.load()["text|g|m"]
        assert row["in_tokens"] == 42 and row["out_tokens"] == 8

    def test_degrades_to_empty_and_noop_without_a_collection(self):
        store = ColUsageStore(db_provider=lambda: None)
        store.save({"text|g|m": _aggregate_row()})  # no-op, no raise
        assert store.load() == {}
        store.ensure()  # no-op, no raise

    def test_buffered_recorder_round_trips_through_col_store(self):
        # End-to-end: the buffered recorder folds calls and flushes into the col.db table.
        db = _SqliteDb()
        rec = BufferedUsageRecorder(
            ColUsageStore(db_provider=lambda: db), schedule_main=_inline
        )
        rec.record(
            kind="text",
            provider="g",
            model="m",
            in_chars=5,
            out_chars=2,
            in_tokens=7,
            out_tokens=1,
        )
        reloaded = ColUsageStore(db_provider=lambda: db).load()["text|g|m"]
        assert reloaded["calls"] == 1
        assert reloaded["in_tokens"] == 7 and reloaded["out_tokens"] == 1


class TestFoldCall:
    """The shared aggregation helper both recorders call."""

    def test_defaults_then_increments_a_new_row(self):
        rows: dict = {}
        _fold_call(
            rows,
            kind="text",
            provider="g",
            model="m",
            in_chars=10,
            out_chars=4,
            in_tokens=3,
            out_tokens=1,
            now=99.0,
        )
        assert rows == {
            "text|g|m": {
                "kind": "text",
                "provider": "g",
                "model": "m",
                "calls": 1,
                "in_chars": 10,
                "out_chars": 4,
                "in_tokens": 3,
                "out_tokens": 1,
                "last_used_ts": 99.0,
            }
        }

    def test_both_recorders_aggregate_identically(self, tmp_path):
        # _fold_call parity: the same sequence yields the same rows through either recorder
        # (last_used_ts aside — Json uses time_fn, Buffered uses time.time()).
        calls = [
            {
                "kind": "text",
                "provider": "g",
                "model": "m",
                "in_chars": 3,
                "out_chars": 1,
                "in_tokens": 5,
                "out_tokens": 2,
            },
            {
                "kind": "text",
                "provider": "g",
                "model": "m",
                "in_chars": 7,
                "out_chars": 4,
                "in_tokens": 6,
                "out_tokens": 3,
            },
            {
                "kind": "sound",
                "provider": "edge",
                "model": "v",
                "in_chars": 2,
                "out_chars": 9,
            },
        ]
        json_rec = JsonUsageRecorder(
            JsonUsageStore(tmp_path / "u.json"), time_fn=lambda: 0.0
        )
        buf_rec = BufferedUsageRecorder(_FakeUsageStore(), schedule_main=_inline)
        for call in calls:
            json_rec.record(**call)
            buf_rec.record(**call)

        def _strip(rows):
            return sorted(
                ({k: v for k, v in r.items() if k != "last_used_ts"} for r in rows),
                key=lambda r: (r["kind"], r["provider"], r["model"]),
            )

        assert _strip(json_rec.snapshot()) == _strip(buf_rec.snapshot())


class TestBufferedUsageRecorder:
    def test_record_then_snapshot_and_flush_persists(self):
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=_inline)
        rec.record(kind="text", provider="gemini", model="m", in_chars=10, out_chars=5)
        rows = rec.snapshot()
        assert len(rows) == 1 and rows[0]["calls"] == 1
        assert rows[0]["in_chars"] == 10 and rows[0]["out_chars"] == 5
        # The inline scheduler flushed the aggregate to the store.
        assert store.saves and store.saves[-1]["text|gemini|m"]["calls"] == 1

    def test_aggregates_repeated_calls_on_one_key(self):
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=_inline)
        rec.record(kind="text", provider="g", model="m", in_chars=3, out_chars=1)
        rec.record(kind="text", provider="g", model="m", in_chars=7, out_chars=2)
        row = rec.snapshot()[0]
        assert row["calls"] == 2 and row["in_chars"] == 10 and row["out_chars"] == 3

    def test_aggregates_in_and_out_tokens(self):
        rec = BufferedUsageRecorder(_FakeUsageStore(), schedule_main=_inline)
        rec.record(
            kind="text",
            provider="g",
            model="m",
            in_chars=1,
            out_chars=1,
            in_tokens=40,
            out_tokens=9,
        )
        rec.record(
            kind="text",
            provider="g",
            model="m",
            in_chars=1,
            out_chars=1,
            in_tokens=2,
            out_tokens=3,
        )
        row = rec.snapshot()[0]
        assert row["in_tokens"] == 42 and row["out_tokens"] == 12

    def test_flush_is_coalesced_across_rapid_records(self):
        # A deferred scheduler queues the flush callback; only ONE should be pending across N
        # records (coalesced), so running the queue saves once with the full aggregate.
        queue: list = []
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=queue.append)
        for _ in range(5):
            rec.record(kind="text", provider="g", model="m", in_chars=1, out_chars=1)
        assert len(queue) == 1  # coalesced: not one flush per record
        assert store.saves == []  # nothing saved until the queued flush runs
        queue[0]()  # the main-thread flush
        assert len(store.saves) == 1
        assert store.saves[0]["text|g|m"]["calls"] == 5

    def test_reschedules_after_a_flush_completes(self):
        queue: list = []
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=queue.append)
        rec.record(kind="text", provider="g", model="m", in_chars=1, out_chars=1)
        queue.pop()()  # flush clears the pending flag
        rec.record(kind="text", provider="g", model="m", in_chars=1, out_chars=1)
        assert len(queue) == 1  # a new flush was scheduled after the first completed

    def test_flush_now_persists_synchronously(self):
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=lambda _cb: None)  # never runs
        rec.record(
            kind="sound", provider="edge_tts", model="v", in_chars=4, out_chars=9
        )
        assert store.saves == []  # the scheduled flush never ran
        rec.flush_now()
        assert store.saves and store.saves[-1]["sound|edge_tts|v"]["calls"] == 1

    def test_loads_initial_aggregate_from_store(self):
        store = _FakeUsageStore(
            {
                "text|g|m": {
                    "kind": "text",
                    "provider": "g",
                    "model": "m",
                    "calls": 4,
                    "in_chars": 8,
                    "out_chars": 0,
                    "in_tokens": 0,
                    "out_tokens": 0,
                    "last_used_ts": None,
                }
            }
        )
        rec = BufferedUsageRecorder(store, schedule_main=_inline)
        # The seeded row is present and further records fold onto it.
        rec.record(kind="text", provider="g", model="m", in_chars=2, out_chars=0)
        row = next(r for r in rec.snapshot() if r["calls"] == 5)
        assert row["in_chars"] == 10

    def test_flush_never_raises_on_a_failing_store(self):
        class _Boom(_FakeUsageStore):
            def save(self, data):
                raise RuntimeError("store down")

        rec = BufferedUsageRecorder(_Boom(), schedule_main=_inline)
        rec.record(
            kind="text", provider="g", model="m", in_chars=1, out_chars=1
        )  # no raise


class TestFlushDefaultRecorder:
    def test_flushes_a_buffered_default(self):
        store = _FakeUsageStore()
        rec = BufferedUsageRecorder(store, schedule_main=lambda _cb: None)
        previous = default_recorder()
        set_default_recorder(rec)
        try:
            rec.record(kind="text", provider="g", model="m", in_chars=1, out_chars=1)
            flush_default_recorder()
            assert store.saves  # the teardown flush persisted the buffered rows
        finally:
            set_default_recorder(previous)

    def test_is_a_noop_for_a_non_buffering_default(self):
        previous = default_recorder()
        set_default_recorder(NullUsageRecorder())
        try:
            flush_default_recorder()  # no flush_now attribute → no-op, no raise
        finally:
            set_default_recorder(previous)
