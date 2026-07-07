"""Tests for the fetched-voice cache backends (offline; tmp_path / fake col — no network)."""

from __future__ import annotations

from omnia.core.providers.tts.base import TTSVoice
from omnia.core.providers.voice_cache import (
    CollectionVoiceCache,
    JsonVoiceCache,
    VoiceCache,
)


def _sample() -> dict[str, list[TTSVoice]]:
    return {
        "edge_tts": [
            TTSVoice(
                "edge_tts", "ja-JP-NanamiNeural", "ja-JP", "Nanami", "Female", "", "ja"
            )
        ]
    }


class _FakeCol:
    """A stand-in collection exposing ``get_config``/``set_config`` over a plain dict."""

    def __init__(self) -> None:
        self.conf: dict[str, object] = {}

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.conf[key] = value


class TestJsonVoiceCache:
    def test_is_a_voice_cache(self, tmp_path):
        assert isinstance(JsonVoiceCache(tmp_path), VoiceCache)

    def test_round_trips_the_provider_voice_map(self, tmp_path):
        cache = JsonVoiceCache(tmp_path)
        cache.save(_sample())
        assert cache.load() == _sample()

    def test_missing_cache_returns_empty(self, tmp_path):
        assert JsonVoiceCache(tmp_path).load() == {}

    def test_corrupt_cache_returns_empty(self, tmp_path):
        (tmp_path / "voices.json").write_text("{not json", encoding="utf-8")
        assert JsonVoiceCache(tmp_path).load() == {}

    def test_non_object_cache_returns_empty(self, tmp_path):
        (tmp_path / "voices.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert JsonVoiceCache(tmp_path).load() == {}


class TestCollectionVoiceCache:
    def test_is_a_voice_cache(self):
        assert isinstance(CollectionVoiceCache(lambda: _FakeCol()), VoiceCache)

    def test_round_trips_via_the_collection_config(self):
        col = _FakeCol()
        cache = CollectionVoiceCache(col_provider=lambda: col)
        cache.save(_sample())
        # Serialized under the "omnia:voices" key as plain dicts, rebuilt into TTSVoice on load.
        assert list(col.conf["omnia:voices"].keys()) == ["edge_tts"]
        assert CollectionVoiceCache(col_provider=lambda: col).load() == _sample()

    def test_load_on_empty_collection_returns_empty(self):
        assert CollectionVoiceCache(col_provider=lambda: _FakeCol()).load() == {}

    def test_load_without_collection_returns_empty(self):
        # A col_provider that fails (e.g. mw.col not ready) degrades to {}, not a crash.
        def boom():
            raise RuntimeError("col not ready")

        cache = CollectionVoiceCache(col_provider=boom)
        assert cache.load() == {}
        cache.save(_sample())  # save is a silent no-op without a collection

    def test_malformed_collection_value_returns_empty(self):
        col = _FakeCol()
        col.conf["omnia:voices"] = [1, 2, 3]  # not a provider→list map
        assert CollectionVoiceCache(col_provider=lambda: col).load() == {}
