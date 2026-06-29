"""Tests for the fetched-voice on-disk cache (offline; tmp_path only — no network)."""

from __future__ import annotations

from omnia.core.providers import voice_cache
from omnia.core.providers.tts.base import TTSVoice


class TestVoiceCache:
    def test_round_trips_the_provider_voice_map(self, tmp_path):
        voices = {
            "edge_tts": [
                TTSVoice(
                    "edge_tts",
                    "ja-JP-NanamiNeural",
                    "ja-JP",
                    "Nanami",
                    "Female",
                    "",
                    "ja",
                )
            ]
        }
        voice_cache.save_cached_voices(tmp_path, voices)
        loaded = voice_cache.load_cached_voices(tmp_path)
        assert loaded == voices  # frozen dataclasses compare by value

    def test_missing_cache_returns_empty(self, tmp_path):
        assert voice_cache.load_cached_voices(tmp_path) == {}

    def test_corrupt_cache_returns_empty(self, tmp_path):
        voice_cache.cache_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert voice_cache.load_cached_voices(tmp_path) == {}

    def test_non_object_cache_returns_empty(self, tmp_path):
        voice_cache.cache_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
        assert voice_cache.load_cached_voices(tmp_path) == {}
