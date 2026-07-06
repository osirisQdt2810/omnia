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
    JsonUsageRecorder,
    NullUsageRecorder,
    RecordingLLMProvider,
    RecordingTTSProvider,
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
    def test_round_trip_aggregates_calls_and_chars(self, tmp_path):
        clock = {"t": 100.0}
        recorder = JsonUsageRecorder(
            tmp_path / "usage.json", time_fn=lambda: clock["t"]
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

    def test_distinct_keys_are_separate_rows(self, tmp_path):
        recorder = JsonUsageRecorder(tmp_path / "usage.json")
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
        recorder = JsonUsageRecorder(tmp_path / "nope.json")
        assert recorder.snapshot() == []

    def test_snapshot_on_corrupt_file_is_empty(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text("not json{", encoding="utf-8")
        assert JsonUsageRecorder(path).snapshot() == []

    def test_dump_is_atomic_via_temp_then_replace(self, tmp_path, monkeypatch):
        # Regression (L3): the write must go through a temp file + os.replace so a mid-write
        # failure can't truncate the existing usage.json to nothing. Make os.replace raise, then
        # confirm the prior content survives intact (the failed write only touched the temp file).
        path = tmp_path / "usage.json"
        recorder = JsonUsageRecorder(path)
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
