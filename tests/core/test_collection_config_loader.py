"""Tests for the collection-backed config loader (ADR-006).

:class:`CollectionConfigLoader` keeps ``omnia``/``features`` in the Anki collection config
(``get_config``/``set_config``) and ``providers.toml`` on disk. A fake collection (a plain dict)
exercises the round-trip; ``tmp_path`` holds ``providers.toml``. The critical invariants: the DB
domains NEVER touch a file (even when a legacy one exists), no collection degrades to defaults,
``ensure_live_files`` seeds ONLY ``providers.toml``, and nothing is migrated/renamed.
"""

from __future__ import annotations

import tomllib

from omnia.core.config.loader import (
    BaseConfigLoader,
    CollectionConfigLoader,
    build_config_loader,
)


class _FakeCol:
    """A stand-in collection exposing ``get_config``/``set_config`` over a plain dict."""

    def __init__(self) -> None:
        self.conf: dict[str, object] = {}

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.conf[key] = value


def _loader(tmp_path, col=None) -> CollectionConfigLoader:
    provider = (lambda: col) if col is not None else (lambda: None)
    return CollectionConfigLoader(tmp_path, col_provider=provider)


class TestBackendSelector:
    def test_default_backend_is_collection(self, tmp_path):
        loader = build_config_loader(tmp_path)
        assert isinstance(loader, CollectionConfigLoader)
        assert isinstance(loader, BaseConfigLoader)

    def test_toml_backend_is_selectable(self, tmp_path):
        from omnia.core.config.loader import TomlConfigLoader

        assert isinstance(
            build_config_loader(tmp_path, backend="toml"), TomlConfigLoader
        )

    def test_unknown_backend_raises(self, tmp_path):
        import pytest

        with pytest.raises(ValueError):
            build_config_loader(tmp_path, backend="nope")


class TestCollectionConfigLoader:
    def test_db_domains_use_namespaced_collection_keys(self):
        assert CollectionConfigLoader._col_key("omnia.toml") == "omnia:config:omnia"
        assert (
            CollectionConfigLoader._col_key("features.toml") == "omnia:config:features"
        )

    def test_db_files_read_write_go_to_collection(self, tmp_path):
        col = _FakeCol()
        loader = _loader(tmp_path, col)
        loader.write_file("omnia.toml", {"log_level": "DEBUG"})
        loader.write_file(
            "features.toml", {"auto_flip": {"delay_question_seconds": 9.0}}
        )
        # Stored under the namespaced collection keys, never on disk.
        assert col.conf["omnia:config:omnia"] == {"log_level": "DEBUG"}
        assert col.conf["omnia:config:features"] == {
            "auto_flip": {"delay_question_seconds": 9.0}
        }
        assert not (tmp_path / "omnia.toml").exists()
        assert not (tmp_path / "features.toml").exists()
        # Read back from the collection.
        assert loader.read_file("omnia.toml") == {"log_level": "DEBUG"}

    def test_providers_stay_on_disk(self, tmp_path):
        loader = _loader(tmp_path, _FakeCol())
        loader.write_file("providers.toml", {"llm": {"provider": "gemini"}})
        assert (tmp_path / "providers.toml").exists()
        assert loader.read_file("providers.toml") == {"llm": {"provider": "gemini"}}

    def test_load_merged_merges_all_three_sources(self, tmp_path):
        col = _FakeCol()
        loader = _loader(tmp_path, col)
        loader.write_file(
            "omnia.toml",
            {"log_level": "DEBUG", "plugins": {"auto_flip": {"enabled": True}}},
        )
        loader.write_file(
            "features.toml", {"auto_flip": {"delay_question_seconds": 4.0}}
        )
        loader.write_file("providers.toml", {"llm": {"provider": "gemini"}})
        merged = loader.load_merged()
        assert merged["log_level"] == "DEBUG"
        assert merged["plugins"]["auto_flip"]["enabled"] is True
        assert merged["auto_flip"]["delay_question_seconds"] == 4.0
        assert merged["llm"]["provider"] == "gemini"

    def test_load_returns_omnia_config_from_collection(self, tmp_path):
        col = _FakeCol()
        loader = _loader(tmp_path, col)
        loader.write_file("omnia.toml", {"log_level": "WARNING"})
        assert loader.load().log_level == "WARNING"

    def test_no_collection_returns_empty_for_db_files_and_never_reads_disk(
        self, tmp_path
    ):
        # A legacy file on disk MUST be ignored — the DB domains only ever come from the
        # collection, so a fileless-but-synced device and a file-carrying dev box agree.
        (tmp_path / "omnia.toml").write_text(
            'log_level = "CRITICAL"\n', encoding="utf-8"
        )
        loader = _loader(tmp_path, col=None)  # no collection
        assert loader.read_file("omnia.toml") == {}
        assert loader.read_file("features.toml") == {}

    def test_col_present_but_key_absent_never_reads_the_legacy_file(self, tmp_path):
        # The realistic new-device branch: a collection IS loaded but has no omnia config key
        # yet, AND a legacy omnia.toml sits on disk. The DB backend must return {} for the DB
        # domain and never read that file — its distinctive content must not leak into the merge.
        (tmp_path / "omnia.toml").write_text(
            'log_level = "CRITICAL"\n', encoding="utf-8"
        )
        loader = _loader(
            tmp_path, _FakeCol()
        )  # col present, no "omnia:config:omnia" key
        assert loader.read_file("omnia.toml") == {}
        assert "CRITICAL" not in repr(loader.load_merged())

    def test_no_collection_write_is_skipped_and_creates_no_file(self, tmp_path):
        loader = _loader(tmp_path, col=None)
        loader.write_file("omnia.toml", {"log_level": "DEBUG"})  # warns + skips
        assert not (tmp_path / "omnia.toml").exists()

    def test_ensure_live_files_seeds_only_providers(self, tmp_path):
        for name in ("omnia", "features", "providers"):
            (tmp_path / f"{name}.example.toml").write_text(
                'seed = "template"\n', encoding="utf-8"
            )
        _loader(tmp_path, _FakeCol()).ensure_live_files()
        assert (tmp_path / "providers.toml").exists()
        assert not (tmp_path / "omnia.toml").exists()
        assert not (tmp_path / "features.toml").exists()

    def test_ensure_live_files_seeds_providers_from_separate_template_dir(
        self, tmp_path
    ):
        # Templates live at the add-on root config/; the live dir is user_files/config. Only
        # providers.toml is seeded, into the LIVE dir, and the template dir is never written.
        template_dir = tmp_path / "templates"
        live_dir = tmp_path / "live"
        template_dir.mkdir()
        for name in ("omnia", "features", "providers"):
            (template_dir / f"{name}.example.toml").write_text(
                'seed = "template"\n', encoding="utf-8"
            )
        loader = CollectionConfigLoader(
            live_dir, template_dir=template_dir, col_provider=lambda: None
        )
        loader.ensure_live_files()
        assert (live_dir / "providers.toml").exists()
        assert not (live_dir / "omnia.toml").exists()
        assert not (live_dir / "features.toml").exists()
        assert not (template_dir / "providers.toml").exists()

    def test_no_migration_or_rename_of_legacy_files(self, tmp_path):
        # A legacy omnia.toml/features.toml must be left byte-for-byte untouched (no .bak,
        # no .migrated, no read-into-DB) — the two worlds are separate.
        legacy_omnia = tmp_path / "omnia.toml"
        legacy_features = tmp_path / "features.toml"
        legacy_omnia.write_text('log_level = "CRITICAL"\n', encoding="utf-8")
        legacy_features.write_text("[auto_flip]\ndelay = 1\n", encoding="utf-8")
        col = _FakeCol()
        loader = _loader(tmp_path, col)
        loader.load_merged()
        # Files unchanged, no sidecar files created, and their content did NOT leak into the DB.
        assert legacy_omnia.read_text(encoding="utf-8") == 'log_level = "CRITICAL"\n'
        assert legacy_features.read_text(encoding="utf-8") == "[auto_flip]\ndelay = 1\n"
        assert not (tmp_path / "omnia.toml.bak").exists()
        assert not (tmp_path / "omnia.toml.migrated").exists()
        assert col.conf == {}  # nothing was imported into the collection

    def test_written_providers_file_is_valid_toml(self, tmp_path):
        loader = _loader(tmp_path, _FakeCol())
        loader.write_file("providers.toml", {"tts": {"provider": "edge_tts"}})
        on_disk = tomllib.loads(
            (tmp_path / "providers.toml").read_text(encoding="utf-8")
        )
        assert on_disk == {"tts": {"provider": "edge_tts"}}
