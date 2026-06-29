"""Tests for the self-registration TTS registry (``core.providers.tts.registry``).

Covers the decorator's duplicate/no-op rules and that ``create_tts_provider`` resolves the
registered classes (including the openai-family multi-name share) with the right config.
"""

from __future__ import annotations

import pytest

from omnia.core.providers import ProviderError, create_tts_provider
from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.edge_tts import EdgeTTS
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.registry import (
    TTS_REGISTRY,
    get_tts,
    register_tts,
    registered_tts_providers,
)


class _DummyTTS(TTSProvider):
    name = "dummy"

    def synthesize(self, text, *, lang=None, voice=None):  # pragma: no cover - unused
        return b""


class TestRegisterTTS:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            register_tts("")(_DummyTTS)

    def test_no_names_raises(self):
        with pytest.raises(ValueError):
            register_tts()

    def test_duplicate_name_different_class_raises(self):
        class _OtherTTS(_DummyTTS):
            pass

        # "edge_tts" is already bound to EdgeTTS; binding a different class must fail.
        with pytest.raises(ValueError):
            register_tts("edge_tts")(_OtherTTS)

    def test_same_class_reregister_is_noop(self):
        before = dict(TTS_REGISTRY)
        # Re-applying the existing binding for EdgeTTS must not raise or change anything.
        register_tts("edge_tts")(EdgeTTS)
        assert before == TTS_REGISTRY

    def test_registered_names_sorted(self):
        names = registered_tts_providers()
        assert names == sorted(names)

    def test_get_tts_unknown_returns_none(self):
        assert get_tts("does-not-exist") is None


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
