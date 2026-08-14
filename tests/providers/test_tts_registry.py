"""Tests for the TTS binding of the provider registry (``core.providers.tts.registry``).

Covers what is TTS-specific: ``tts_providers_with_ext`` (the audio-format query, which has no
LLM analogue) and that ``create_tts_provider`` resolves the registered classes — including the
openai-family multi-name share — with the right config. The registration/duplicate/no-op rules
themselves are generic and live in ``test_provider_registry.py``, which asserts them for BOTH
kinds against the shared mechanism.
"""

from __future__ import annotations

import pytest

from omnia.core.providers import ProviderError, create_tts_provider
from omnia.core.providers.tts.edge_tts import EdgeTTS
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.registry import (
    registered_tts_providers,
    tts_providers_with_ext,
)


class TestProvidersByFormat:
    """``tts_providers_with_ext`` is the one place that knows which voices return what.

    Audio surgery (smart_notes' ``cloze_audio``) can only splice PCM, so it has to TELL the
    user which voices work without a codec runtime. That list is derived here rather than
    written out anywhere, because a hand-kept copy goes stale the moment a provider is added
    or renamed — and then it lies in the UI.
    """

    def test_every_registered_provider_lands_in_exactly_one_format(self):
        wav = tts_providers_with_ext("wav")
        mp3 = tts_providers_with_ext("mp3")
        assert set(wav) | set(mp3) == set(registered_tts_providers())
        assert not set(wav) & set(mp3)

    def test_the_offline_engines_are_the_wav_ones(self):
        assert tts_providers_with_ext("wav") == ["piper", "viettts"]

    def test_names_are_sorted_and_include_every_alias_of_a_shared_class(self):
        mp3 = tts_providers_with_ext("mp3")
        assert mp3 == sorted(mp3)
        # The openai family is one class under three config names; a user configures a NAME.
        assert {"openai", "openai_compatible", "openrouter"} <= set(mp3)

    def test_an_unknown_format_matches_nothing(self):
        assert tts_providers_with_ext("flac") == []


class TestCreateTTSProvider:
    def test_builds_edge_tts(self):
        provider = create_tts_provider({"provider": "edge_tts"})
        assert isinstance(provider, EdgeTTS)

    def test_openrouter_uses_openrouter_base_url(self):
        provider = create_tts_provider({"provider": "openrouter", "api_key": "k"})
        assert isinstance(provider, OpenAICompatibleTTS)
        assert provider._base_url == "https://openrouter.ai/api/v1"

    def test_openai_uses_openai_base_url(self):
        provider = create_tts_provider({"provider": "openai", "api_key": "k"})
        assert provider._base_url == "https://api.openai.com/v1"

    def test_unknown_provider_raises(self):
        with pytest.raises(ProviderError):
            create_tts_provider({"provider": "nope"})
