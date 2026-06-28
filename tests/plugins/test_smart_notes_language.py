"""Tests for TTS language detection: the raw detect_language + the best-effort LanguageDetector.

Also covers the TTS generation path: with no pinned voice the detected language is threaded
into ``synthesize``; a pinned voice skips detection; a detection failure is swallowed.
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider, real_llm_provider_or_skip

from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.config import SmartNotesFieldRule
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.engine.language import LanguageDetector, detect_language


class _CodeLLM(FakeLLMProvider):
    """A fake LLM that returns a fixed language code for every call."""

    def __init__(self, code: str) -> None:
        super().__init__(text=code)


class _BoomLLM(FakeLLMProvider):
    def generate_text(self, *a, **k):
        raise ProviderError("boom")


class _Hub:
    """Minimal ProviderHub-shaped stub exposing ``llm()`` / ``tts()``."""

    def __init__(self, llm=None, tts=None) -> None:
        self._llm = llm
        self._tts = tts

    def llm(self, *, model: str = "", provider: str = ""):
        return self._llm

    def tts(self):
        return self._tts


class _RecordingTTS(FakeTTSProvider):
    """Records (text, lang, voice) for each synthesize call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    def synthesize(self, text, *, lang=None, voice=None):
        self.calls.append((text, lang, voice))
        return b"AUDIO"


# ---------------------------------------------------------------------------
# detect_language (raw call).
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    def test_returns_clean_code(self):
        assert detect_language(_CodeLLM("vi"), "xin chào") == "vi"

    def test_uppercase_reply_is_normalised(self):
        assert detect_language(_CodeLLM("VI"), "xin chào") == "vi"

    def test_blank_text_returns_fallback_without_calling(self):
        class _NoCall(FakeLLMProvider):
            def generate_text(self, *a, **k):
                raise AssertionError("must not call the LLM for blank text")

        assert detect_language(_NoCall(), "   ", fallback="en") == "en"


# ---------------------------------------------------------------------------
# LanguageDetector (best-effort wrapper).
# ---------------------------------------------------------------------------


class TestLanguageDetector:
    def test_returns_detected_code(self):
        assert LanguageDetector().detect(_Hub(llm=_CodeLLM("vi")), "xin chào") == "vi"

    def test_disabled_returns_none(self):
        assert (
            LanguageDetector(enabled=False).detect(_Hub(llm=_CodeLLM("vi")), "text")
            is None
        )

    def test_blank_text_returns_none(self):
        assert LanguageDetector().detect(_Hub(llm=_CodeLLM("vi")), "   ") is None

    def test_provider_failure_is_swallowed(self):
        assert LanguageDetector().detect(_Hub(llm=_BoomLLM()), "text") is None


@pytest.mark.llm
class TestLanguageDetectorReal:
    def test_detects_a_two_letter_code(self):
        provider = real_llm_provider_or_skip()
        code = LanguageDetector().detect(
            _Hub(llm=provider), "This is plain English text."
        )
        assert code is None or (isinstance(code, str) and len(code) == 2)


# ---------------------------------------------------------------------------
# TTS generation path (Generator strategy threads the detected language).
# ---------------------------------------------------------------------------


class TestTTSLanguagePath:
    def test_no_voice_detects_and_passes_language(self):
        tts = _RecordingTTS()
        service = GenerationService(_Hub(llm=_CodeLLM("vi"), tts=tts))
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "xin chào"})
        assert tts.calls == [("xin chào", "vi", None)]

    def test_pinned_voice_skips_detection(self):
        tts = _RecordingTTS()

        class _NoDetect(FakeLLMProvider):
            def generate_text(self, *a, **k):
                raise AssertionError("must not detect when a voice is pinned")

        service = GenerationService(_Hub(llm=_NoDetect(), tts=tts))
        rule = SmartNotesFieldRule(
            kind="tts", prompt="{{Word}}", target_field="Audio", voice="en-US-X"
        )
        service.generate(rule, {"Word": "hi"})
        assert tts.calls == [("hi", None, "en-US-X")]

    def test_detection_failure_falls_back_to_no_language(self):
        tts = _RecordingTTS()
        service = GenerationService(_Hub(llm=_BoomLLM(), tts=tts))
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "hi"})
        assert tts.calls == [("hi", None, None)]

    def test_detection_disabled_passes_no_language(self):
        tts = _RecordingTTS()
        service = GenerationService(
            _Hub(llm=_CodeLLM("vi"), tts=tts), detect_tts_language=False
        )
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "xin chào"})
        assert tts.calls == [("xin chào", None, None)]

    def test_explicit_language_skips_detection(self):
        tts = _RecordingTTS()

        class _NoDetect(FakeLLMProvider):
            def generate_text(self, *a, **k):
                raise AssertionError("must not detect when a language is set")

        service = GenerationService(_Hub(llm=_NoDetect(), tts=tts))
        rule = SmartNotesFieldRule(
            kind="tts", prompt="{{Word}}", target_field="Audio", language="vi"
        )
        service.generate(rule, {"Word": "x"})
        assert tts.calls == [("x", "vi", None)]

    def test_voice_overrides_language(self):
        tts = _RecordingTTS()
        service = GenerationService(_Hub(llm=_CodeLLM("vi"), tts=tts))
        rule = SmartNotesFieldRule(
            kind="tts",
            prompt="{{Word}}",
            target_field="Audio",
            voice="en-US-X",
            language="vi",
        )
        service.generate(rule, {"Word": "x"})
        assert tts.calls == [("x", None, "en-US-X")]  # voice wins; no lang threaded
