"""Tests for the typed config layer (models, loader, repository)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import omnia.plugins  # noqa: F401 — registers plugins so feature_settings resolves config_model
from omnia.core.config import ConfigLoader, ConfigRepository
from omnia.core.config.loader import ConfigLoader as Loader
from omnia.core.config.models import OmniaConfig
from omnia.plugins.typed_accuracy.config import TypedAccuracySettings


class TestConfigRepository:
    def test_loads_bundled_defaults(self, config_repo):
        cfg = config_repo.config
        # Per-feature settings now come from each plugin's own model (resolved via the
        # registry), not typed fields on OmniaConfig.
        assert config_repo.feature_settings("auto_flip").delay_question_seconds == 3.0
        assert config_repo.feature_settings("typed_accuracy").threshold == 0.7
        assert config_repo.feature_settings("typed_accuracy").pass_ease == "good"
        assert config_repo.feature_settings("overdue_guard").min_days == 2
        assert cfg.llm.provider == "gemini_vertex"
        assert cfg.tts.provider == "google_translate"
        # Vertex auth now lives in the per-provider [llm.gemini_vertex] subsection.
        assert cfg.llm.gemini_vertex.location == "global"
        assert cfg.llm.gemini_vertex.text_model == "gemini-2.5-flash"
        assert cfg.llm.active() is cfg.llm.gemini_vertex
        # TTS uses the same per-provider shape.
        assert cfg.tts.google_translate.lang == "en"
        assert cfg.tts.active() is cfg.tts.google_translate

    def test_all_plugins_default_disabled(self, config_repo):
        for pid in (
            "auto_flip",
            "typed_accuracy",
            "display_interval",
            "overdue_guard",
            "smart_notes",
        ):
            assert config_repo.is_enabled(pid) is False

    def test_set_enabled_persists_and_reloads(self, config_repo, tmp_path):
        config_repo.set_enabled("auto_flip", True)
        assert config_repo.is_enabled("auto_flip") is True
        # A brand-new repository over the same user file sees the persisted override.
        user_file = tmp_path / "omnia.toml"
        fresh = ConfigRepository(ConfigLoader(_config_dir(), user_file))
        assert fresh.is_enabled("auto_flip") is True

    def test_update_section_changes_typed_settings(self, config_repo):
        config_repo.update_section("auto_flip", {"delay_question_seconds": 5.5})
        settings = config_repo.feature_settings("auto_flip")
        assert settings is not None
        assert settings.delay_question_seconds == 5.5  # type: ignore[attr-defined]

    def test_feature_settings_none_for_unknown(self, config_repo):
        assert config_repo.feature_settings("not_a_plugin") is None

    def test_feature_settings_returns_typed_model_from_namespace(self, tmp_path):
        # The plugin's OWN config_model parses its raw [plugin] namespace from the merged dict.
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        user_file = tmp_path / "omnia.toml"
        user_file.write_text("[typed_accuracy]\nthreshold = 0.42\n", encoding="utf-8")
        repo = ConfigRepository(ConfigLoader(_config_dir(), user_file))
        settings = repo.feature_settings("typed_accuracy")
        assert isinstance(settings, TypedAccuracySettings)
        assert settings.threshold == 0.42

    def test_feature_settings_rejects_unknown_keys(self, tmp_path):
        # The plugin model is extra="forbid", so a typo in its namespace must raise.
        user_file = tmp_path / "omnia.toml"
        user_file.write_text("[overdue_guard]\nnot_a_real_key = 5\n", encoding="utf-8")
        repo = ConfigRepository(ConfigLoader(_config_dir(), user_file))
        with pytest.raises(ValidationError):
            repo.feature_settings("overdue_guard")

    def test_user_override_wins_over_default(self, tmp_path):
        user_file = tmp_path / "omnia.toml"
        user_file.write_text("[typed_accuracy]\nthreshold = 0.9\n", encoding="utf-8")
        repo = ConfigRepository(ConfigLoader(_config_dir(), user_file))
        assert repo.feature_settings("typed_accuracy").threshold == 0.9
        # untouched defaults still present
        assert repo.feature_settings("auto_flip").delay_question_seconds == 3.0


class TestConfigModels:
    def test_model_validation_rejects_bad_pass_ease(self):
        with pytest.raises(ValidationError):
            TypedAccuracySettings(pass_ease="medium")

    def test_model_validation_rejects_out_of_range_threshold(self):
        with pytest.raises(ValidationError):
            TypedAccuracySettings(threshold=1.5)

    def test_omnia_config_ignores_unknown_top_level_key(self):
        cfg = OmniaConfig.parse_obj({"unknown_future_key": 123, "log_level": "DEBUG"})
        assert cfg.log_level == "DEBUG"


class TestConfigLoader:
    def test_deep_merge_is_recursive(self):
        base = {"a": {"x": 1, "y": 2}, "b": 1}
        override = {"a": {"y": 3, "z": 4}, "c": 5}
        merged = Loader._deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}
        assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # base untouched


def _config_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.parent / "src" / "omnia" / "config"
