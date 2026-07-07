"""Tests for the env-driven persistence dispatch + sync-on-change layer (ADR-006).

:class:`~omnia.core.config.dispatch.PersistenceDispatcher` picks each concern's backend from the
``OMNIA_*_STORAGE`` knobs and, when a knob changed since last startup, copies that concern's data
from the previously-used backend into the new one. Everything runs headless: the env knobs are
driven with ``monkeypatch.setenv``, the collection/DB backends are fed a fake ``mw.col`` (a plain
dict for config/voices, an in-memory sqlite for usage), and the marker + file backends live under
a ``tmp_path`` ``user_files`` dir. No real Anki is touched.
"""

from __future__ import annotations

import json
import os
import types

import pytest

from omnia.core.config.dispatch import PersistenceDispatcher
from omnia.core.config.loader import CollectionConfigLoader, TomlConfigLoader
from omnia.core.providers.tts.base import TTSVoice
from omnia.core.providers.usage import (
    BufferedUsageRecorder,
    ColUsageStore,
    JsonUsageRecorder,
    JsonUsageStore,
)
from omnia.core.providers.voice_cache import CollectionVoiceCache, JsonVoiceCache


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


class _FakeCol:
    """A stand-in collection: dict-backed ``get_config``/``set_config`` + a sqlite ``db``."""

    def __init__(self) -> None:
        self.conf: dict[str, object] = {}
        self.db = _SqliteDb()

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.conf[key] = value


def _usage_row(**over) -> dict:
    """A full usage aggregate row (the nine persisted fields), overridable per test."""
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


def _voice_sample() -> dict[str, list[TTSVoice]]:
    return {
        "edge_tts": [
            TTSVoice(
                "edge_tts", "ja-JP-NanamiNeural", "ja-JP", "Nanami", "Female", "", "ja"
            )
        ]
    }


def _write_marker(user_files, data: dict) -> None:
    user_files.mkdir(parents=True, exist_ok=True)
    (user_files / ".storage.json").write_text(json.dumps(data), encoding="utf-8")


def _read_marker(user_files) -> dict:
    return json.loads((user_files / ".storage.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_storage_env(monkeypatch):
    """Start each test with all storage knobs unset (so the default 'database' applies)."""
    for key in (
        "OMNIA_CONFIG_STORAGE",
        "OMNIA_USAGE_STORAGE",
        "OMNIA_VOICE_CACHE_STORAGE",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def user_files(tmp_path):
    path = tmp_path / "user_files"
    path.mkdir()
    return path


@pytest.fixture
def config_dir(tmp_path):
    path = tmp_path / "config"
    path.mkdir()
    return path


@pytest.fixture
def fake_col(monkeypatch):
    """Install a fake ``mw.col`` (dict config + sqlite db) that the DB backends resolve lazily."""
    import aqt

    col = _FakeCol()
    monkeypatch.setattr(aqt, "mw", types.SimpleNamespace(col=col))
    return col


class TestMarker:
    def test_missing_marker_is_empty(self, user_files):
        assert PersistenceDispatcher(user_files)._marker == {}

    def test_corrupt_marker_is_empty(self, user_files):
        (user_files / ".storage.json").write_text("{not json", encoding="utf-8")
        assert PersistenceDispatcher(user_files)._marker == {}

    def test_non_object_marker_is_empty(self, user_files):
        (user_files / ".storage.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert PersistenceDispatcher(user_files)._marker == {}

    def test_round_trips_a_full_marker(self, user_files):
        data = {"config": "toml", "usage": "json", "voices": "database"}
        _write_marker(user_files, data)
        assert PersistenceDispatcher(user_files)._marker == data

    def test_save_then_reload_persists(self, user_files):
        dispatcher = PersistenceDispatcher(user_files)
        dispatcher._marker["config"] = "toml"
        dispatcher._save_marker()
        assert PersistenceDispatcher(user_files)._marker == {"config": "toml"}

    def test_missing_user_files_dir_is_created_on_save(self, tmp_path):
        target = tmp_path / "nonexistent" / "user_files"
        dispatcher = PersistenceDispatcher(target)
        dispatcher._marker["usage"] = "json"
        dispatcher._save_marker()
        assert (target / ".storage.json").exists()

    def test_failed_marker_write_does_not_raise(self, user_files, monkeypatch):
        dispatcher = PersistenceDispatcher(user_files)
        dispatcher._marker["config"] = "toml"

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        dispatcher._save_marker()  # tolerant: logs + continues, never raises into bootstrap


class TestFirstRun:
    def test_config_returns_database_backend_and_records_marker(
        self, user_files, config_dir
    ):
        loader = PersistenceDispatcher(user_files).config_loader(config_dir)
        assert isinstance(loader, CollectionConfigLoader)
        assert _read_marker(user_files)["config"] == "database"

    def test_config_first_run_never_builds_the_old_backend(
        self, user_files, config_dir, monkeypatch
    ):
        built: list = []

        class _SpyToml(TomlConfigLoader):
            def __init__(self, *a, **k) -> None:
                built.append(1)
                super().__init__(*a, **k)

        monkeypatch.setattr("omnia.core.config.loader.TomlConfigLoader", _SpyToml)
        PersistenceDispatcher(user_files).config_loader(config_dir)
        assert (
            built == []
        )  # no marker → no sync → the other backend is never constructed

    def test_usage_returns_buffered_recorder_and_records_marker(self, user_files):
        recorder = PersistenceDispatcher(user_files).usage_recorder()
        assert isinstance(recorder, BufferedUsageRecorder)
        assert _read_marker(user_files)["usage"] == "database"

    def test_voices_returns_collection_cache_and_records_marker(self, user_files):
        cache = PersistenceDispatcher(user_files).voice_cache()
        assert isinstance(cache, CollectionVoiceCache)
        assert _read_marker(user_files)["voices"] == "database"


class TestUsageChangeDetected:
    def test_json_to_database_copies_the_aggregate(
        self, user_files, fake_col, monkeypatch
    ):
        seeded = {"text|gemini|m": _usage_row()}
        JsonUsageStore(user_files / "usage.json").save(seeded)
        _write_marker(user_files, {"usage": "json"})
        monkeypatch.setenv("OMNIA_USAGE_STORAGE", "database")

        recorder = PersistenceDispatcher(user_files).usage_recorder()

        assert isinstance(recorder, BufferedUsageRecorder)
        assert ColUsageStore(db_provider=lambda: fake_col.db).load() == seeded
        assert _read_marker(user_files)["usage"] == "database"

    def test_database_to_json_copies_the_aggregate(
        self, user_files, fake_col, monkeypatch
    ):
        seeded = {"text|gemini|m": _usage_row()}
        ColUsageStore(db_provider=lambda: fake_col.db).save(seeded)
        _write_marker(user_files, {"usage": "database"})
        monkeypatch.setenv("OMNIA_USAGE_STORAGE", "json")

        recorder = PersistenceDispatcher(user_files).usage_recorder()

        assert isinstance(recorder, JsonUsageRecorder)
        assert JsonUsageStore(user_files / "usage.json").load() == seeded
        assert _read_marker(user_files)["usage"] == "json"


class TestVoicesChangeDetected:
    def test_json_to_database_copies_the_voice_map(
        self, user_files, fake_col, monkeypatch
    ):
        JsonVoiceCache(user_files).save(_voice_sample())
        _write_marker(user_files, {"voices": "json"})
        monkeypatch.setenv("OMNIA_VOICE_CACHE_STORAGE", "database")

        cache = PersistenceDispatcher(user_files).voice_cache()

        assert isinstance(cache, CollectionVoiceCache)
        assert (
            CollectionVoiceCache(col_provider=lambda: fake_col).load()
            == _voice_sample()
        )
        assert _read_marker(user_files)["voices"] == "database"

    def test_database_to_json_copies_the_voice_map(
        self, user_files, fake_col, monkeypatch
    ):
        CollectionVoiceCache(col_provider=lambda: fake_col).save(_voice_sample())
        _write_marker(user_files, {"voices": "database"})
        monkeypatch.setenv("OMNIA_VOICE_CACHE_STORAGE", "json")

        cache = PersistenceDispatcher(user_files).voice_cache()

        assert isinstance(cache, JsonVoiceCache)
        assert JsonVoiceCache(user_files).load() == _voice_sample()
        assert _read_marker(user_files)["voices"] == "json"


class TestNoChange:
    def test_equal_marker_never_builds_the_old_backend(
        self, user_files, config_dir, monkeypatch
    ):
        _write_marker(user_files, {"config": "database"})
        monkeypatch.setenv("OMNIA_CONFIG_STORAGE", "database")
        built: list = []

        class _SpyToml(TomlConfigLoader):
            def __init__(self, *a, **k) -> None:
                built.append(1)
                super().__init__(*a, **k)

        monkeypatch.setattr("omnia.core.config.loader.TomlConfigLoader", _SpyToml)
        PersistenceDispatcher(user_files).config_loader(config_dir)
        assert (
            built == []
        )  # marker == env → no sync, so the other backend is never built


class TestConfigSync:
    def test_copies_omnia_and_features_but_not_providers(
        self, user_files, config_dir, fake_col, monkeypatch
    ):
        old = TomlConfigLoader(config_dir)
        old.write_file("omnia.toml", {"log_level": "DEBUG"})
        old.write_file("features.toml", {"auto_flip": {"delay_question_seconds": 4.0}})
        old.write_file("providers.toml", {"llm": {"provider": "gemini"}})
        _write_marker(user_files, {"config": "toml"})
        monkeypatch.setenv("OMNIA_CONFIG_STORAGE", "database")

        written: list[str] = []
        original = CollectionConfigLoader.write_file

        def _spy(self, name, data):
            written.append(name)
            return original(self, name, data)

        monkeypatch.setattr(CollectionConfigLoader, "write_file", _spy)

        PersistenceDispatcher(user_files).config_loader(config_dir)

        # Only omnia + features are copied; providers.toml (same file in both backends) is not.
        assert written == ["omnia.toml", "features.toml"]
        assert fake_col.conf["omnia:config:omnia"] == {"log_level": "DEBUG"}
        assert fake_col.conf["omnia:config:features"] == {
            "auto_flip": {"delay_question_seconds": 4.0}
        }
        assert "omnia:config:providers" not in fake_col.conf
        assert _read_marker(user_files)["config"] == "database"


class TestInvalidEnv:
    def test_invalid_config_value_falls_back_to_database(
        self, user_files, config_dir, monkeypatch
    ):
        monkeypatch.setenv("OMNIA_CONFIG_STORAGE", "garbage")
        loader = PersistenceDispatcher(user_files).config_loader(config_dir)
        assert isinstance(loader, CollectionConfigLoader)
        assert _read_marker(user_files)["config"] == "database"

    def test_invalid_usage_value_falls_back_to_database(self, user_files, monkeypatch):
        monkeypatch.setenv("OMNIA_USAGE_STORAGE", "sqlite")
        recorder = PersistenceDispatcher(user_files).usage_recorder()
        assert isinstance(recorder, BufferedUsageRecorder)
        assert _read_marker(user_files)["usage"] == "database"
