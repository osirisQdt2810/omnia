"""Tests for Auto-detect voice resolution, including when detection produced nothing."""

from __future__ import annotations

import types

import pytest

from omnia.core.providers import ProviderError, ProviderHub


def _hub(auto_voices: dict) -> ProviderHub:
    """A hub carrying only the TTS settings the resolver reads.

    ``_tts_settings`` is a property backed by ``_tts_settings_static``, so the static field is
    what a test sets — going through the real constructor keeps this honest about the wiring.
    """
    hub = ProviderHub.__new__(ProviderHub)
    # ``_tts_settings`` is a property that prefers a live repo (``_config``) and falls back to the
    # static snapshot; with no repo the snapshot is what it reads.
    hub._config = None
    hub._tts_settings_static = types.SimpleNamespace(auto_voices=auto_voices)
    return hub


class TestLanguageKnown:
    def test_maps_a_language_to_its_provider_and_voice(self):
        assert _hub({"en": "edge_tts:en-US-AriaNeural"}).resolve_auto_voice("en") == (
            "edge_tts",
            "en-US-AriaNeural",
        )

    def test_a_language_only_provider_may_have_an_empty_voice(self):
        assert _hub({"vi": "google_translate:"}).resolve_auto_voice("vi") == (
            "google_translate",
            "",
        )

    def test_an_unmapped_language_names_that_language(self):
        with pytest.raises(ProviderError, match="'fr'"):
            _hub({"en": "edge_tts:a"}).resolve_auto_voice("fr")


class TestLanguageUnknown:
    """Detection can yield nothing — no text provider configured, a network error, blank text."""

    def test_a_single_configured_voice_is_used_without_guessing(self):
        # Only one Auto-detect voice exists, so there is nothing to choose between; failing here
        # would be pedantic rather than safe.
        assert _hub({"en": "edge_tts:en-US-AriaNeural"}).resolve_auto_voice("") == (
            "edge_tts",
            "en-US-AriaNeural",
        )

    def test_blank_language_is_treated_the_same_as_missing(self):
        assert _hub({"en": "edge_tts:v"}).resolve_auto_voice("   ") == ("edge_tts", "v")

    def test_several_voices_report_the_REAL_problem(self):
        # The old message blamed the Auto-detect map for language '' — pointing the user at the
        # wrong setting entirely. It must say detection failed.
        with pytest.raises(ProviderError, match="Could not detect the language"):
            _hub({"en": "edge_tts:a", "vi": "edge_tts:b"}).resolve_auto_voice("")

    def test_no_voices_configured_also_reports_detection(self):
        with pytest.raises(ProviderError, match="Could not detect the language"):
            _hub({}).resolve_auto_voice("")

    def test_the_message_never_mentions_an_empty_language(self):
        with pytest.raises(ProviderError) as excinfo:
            _hub({"en": "a:b", "vi": "c:d"}).resolve_auto_voice("")
        assert "''" not in str(excinfo.value)
