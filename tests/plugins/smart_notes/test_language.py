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
    """Minimal ProviderHub-shaped stub exposing ``llm()`` / ``tts(provider=)`` / resolve.

    ``auto_voices`` maps a detected language code to ``(provider, voice)`` for the Auto-detect
    path; unmapped languages raise (mirroring the real hub).
    """

    def __init__(self, llm=None, tts=None, auto_voices=None) -> None:
        self._llm = llm
        self._tts = tts
        self._auto_voices = auto_voices or {}

    def llm(self, *, model: str = "", provider: str = ""):
        return self._llm

    def tts(self, *, provider: str = ""):
        return self._tts

    def resolve_auto_voice(self, lang: str, *, reason: str = ""):
        if lang not in self._auto_voices:
            raise ProviderError(f"No Auto-detect voice set for language {lang!r}")
        return self._auto_voices[lang]


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

    def test_prose_reply_without_a_code_falls_back(self):
        # "Spanish" has no standalone two-letter token → no ISO code parsed → fallback.
        assert detect_language(_CodeLLM("Spanish"), "hola", fallback="und") == "und"

    def test_code_embedded_in_prose_is_extracted(self):
        assert detect_language(_CodeLLM("Language: en"), "hello") == "en"

    def test_bare_code_is_returned(self):
        assert detect_language(_CodeLLM("de"), "hallo") == "de"


# ---------------------------------------------------------------------------
# LanguageDetector (best-effort wrapper).
# ---------------------------------------------------------------------------


class TestLanguageDetector:
    def test_returns_detected_code(self):
        guess = LanguageDetector().detect(_Hub(llm=_CodeLLM("vi")), "xin chào")

        assert guess.code == "vi"
        assert guess.reason == ""  # nothing to report when it worked

    def test_disabled_returns_no_code(self):
        guess = LanguageDetector(enabled=False).detect(_Hub(llm=_CodeLLM("vi")), "text")

        assert guess.code is None
        assert guess.reason == ""  # not a failure — there was nothing to do

    def test_blank_text_returns_no_code(self):
        assert LanguageDetector().detect(_Hub(llm=_CodeLLM("vi")), "   ").code is None

    def test_a_provider_failure_is_swallowed_but_its_reason_is_not(self):
        """Best-effort must not mean silent.

        The failure this pins was reported as "Auto-detect fails on every tts field": the model
        spent its whole token budget thinking and returned no text, the detector swallowed that
        precise message, and the user was told to configure a text provider they had already
        configured. Drop the reason and the only description of what went wrong is gone.
        """
        guess = LanguageDetector().detect(_Hub(llm=_BoomLLM()), "text")

        assert guess.code is None
        assert "boom" in guess.reason


@pytest.mark.llm
class TestLanguageDetectorReal:
    def test_detects_a_two_letter_code(self):
        provider = real_llm_provider_or_skip()
        guess = LanguageDetector().detect(
            _Hub(llm=provider), "This is plain English text."
        )
        # The reason is asserted too: a live model that answers nothing must say WHY here, or
        # the failure the wrapper swallows is invisible in exactly the case it was reported in.
        assert guess.reason == "", guess.reason
        assert guess.code is None or (
            isinstance(guess.code, str) and len(guess.code) == 2
        )


# ---------------------------------------------------------------------------
# TTS generation path (Generator strategy threads the detected language).
# ---------------------------------------------------------------------------


class TestTTSLanguagePath:
    def test_no_voice_detects_and_resolves_language_to_a_voice(self):
        tts = _RecordingTTS()
        # Auto-detect: the detected language ("vi") is threaded AND drives the map lookup.
        service = GenerationService(
            _Hub(
                llm=_CodeLLM("vi"),
                tts=tts,
                auto_voices={"vi": ("edge_tts", "vi-VN-HoaiMyNeural")},
            )
        )
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "xin chào"})
        assert tts.calls == [("xin chào", "vi", "vi-VN-HoaiMyNeural")]

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

    def test_detection_failure_resolves_the_empty_language(self):
        tts = _RecordingTTS()
        # A swallowed detection failure → no language ("") → the map's "" fallback resolves it.
        service = GenerationService(
            _Hub(
                llm=_BoomLLM(),
                tts=tts,
                auto_voices={"": ("google_translate", "")},
            )
        )
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "hi"})
        # Empty resolved voice → None so the provider uses the language directly.
        assert tts.calls == [("hi", None, None)]

    def test_detection_disabled_resolves_the_empty_language(self):
        tts = _RecordingTTS()
        service = GenerationService(
            _Hub(
                llm=_CodeLLM("vi"),
                tts=tts,
                auto_voices={"": ("edge_tts", "en-US-AriaNeural")},
            ),
            detect_tts_language=False,
        )
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        service.generate(rule, {"Word": "xin chào"})
        assert tts.calls == [("xin chào", None, "en-US-AriaNeural")]

    def test_explicit_language_skips_detection(self):
        tts = _RecordingTTS()

        class _NoDetect(FakeLLMProvider):
            def generate_text(self, *a, **k):
                raise AssertionError("must not detect when a language is set")

        service = GenerationService(
            _Hub(
                llm=_NoDetect(),
                tts=tts,
                auto_voices={"vi": ("edge_tts", "vi-VN-HoaiMyNeural")},
            )
        )
        rule = SmartNotesFieldRule(
            kind="tts", prompt="{{Word}}", target_field="Audio", language="vi"
        )
        service.generate(rule, {"Word": "x"})
        assert tts.calls == [("x", "vi", "vi-VN-HoaiMyNeural")]

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


class TestAnExplicitLanguageCostsNothing:
    """A field with its Language set must not pay for detecting one.

    ``for_rule`` used to call the detector unconditionally and then discard the answer on the
    very next line whenever ``rule.language`` was set — one LLM round trip per note, per tts
    field, for a question already answered in the config.
    """

    def test_the_detector_is_never_asked(self):
        from types import SimpleNamespace

        from omnia.plugins.smart_notes.engine.generators import ResolvedVoice

        class _ForbiddenDetector:
            def detect(self, providers, text):
                raise AssertionError("detection ran despite an explicit Language")

        picked: dict[str, object] = {}

        class _Hub:
            def resolve_auto_voice(self, lang, *, reason=""):
                picked["lang"], picked["reason"] = lang, reason
                return "google_translate", ""

            def tts(self, provider=None):
                return SimpleNamespace(audio_ext="mp3")

        rule = SimpleNamespace(voice="", provider="", language="vi")

        ResolvedVoice.for_rule(_Hub(), _ForbiddenDetector(), rule, "bất kỳ câu nào")

        assert picked["lang"] == "vi"
        assert picked["reason"] == ""  # nothing failed, so there is no reason to carry
