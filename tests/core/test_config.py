"""Tests for the typed config layer (models, loader, repository)."""

from __future__ import annotations

try:  # stdlib on Python 3.11+; the vendored tomli covers 3.10 (same fallback as loader.py)
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

from pathlib import Path

import pytest
from pydantic import ValidationError

import omnia.plugins  # noqa: F401 — registers plugins so feature_settings resolves config_model
from omnia.core.config import ConfigLoader, ConfigRepository
from omnia.core.config.loader import deep_merge
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

    def test_set_enabled_persists_and_reloads(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_enabled("auto_flip", True)
        assert repo.is_enabled("auto_flip") is True
        # A brand-new repository over the same config dir sees the persisted change
        # (it now lives in omnia.toml's [plugins.auto_flip] section).
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert fresh.is_enabled("auto_flip") is True

    def test_update_section_changes_typed_settings(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.update_section("auto_flip", {"delay_question_seconds": 5.5})
        settings = repo.feature_settings("auto_flip")
        assert settings is not None
        assert settings.delay_question_seconds == 5.5  # type: ignore[attr-defined]
        # The change persisted to features.toml: a fresh repo reads it back.
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert fresh.feature_settings("auto_flip").delay_question_seconds == 5.5

    def test_feature_settings_none_for_unknown(self, config_repo):
        assert config_repo.feature_settings("not_a_plugin") is None

    def test_feature_settings_returns_typed_model_from_namespace(self, tmp_path):
        # The plugin's OWN config_model parses its raw [plugin] namespace from the merged dict.
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.update_section("typed_accuracy", {"threshold": 0.42})
        settings = repo.feature_settings("typed_accuracy")
        assert isinstance(settings, TypedAccuracySettings)
        assert settings.threshold == 0.42

    def test_feature_settings_tolerates_unknown_keys(self, tmp_path):
        """An unknown key in a plugin's namespace must NOT break that plugin's settings.

        Changed deliberately (was: asserts a ``ValidationError``). ``features.toml`` lives in
        the synced collection config (ADR-006/ADR-008), so an unknown key is normally a key a
        NEWER Omnia wrote on another device — and this call sits inside ``PluginManager``'s
        activation try-block, so raising meant the feature silently never enabled on the older
        device. The plugin models are ``PersistedModel``s now: the key rides along untouched
        and the known settings still parse.
        """
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.update_section("overdue_guard", {"from_a_newer_omnia": 5})
        settings = repo.feature_settings("overdue_guard")
        assert settings is not None
        assert settings.min_days == 2  # type: ignore[attr-defined]
        assert settings.dict()["from_a_newer_omnia"] == 5  # round-trips, never dropped

    def test_edited_value_wins_over_default(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.update_section("typed_accuracy", {"threshold": 0.9})
        assert repo.feature_settings("typed_accuracy").threshold == 0.9
        # untouched defaults still present
        assert repo.feature_settings("auto_flip").delay_question_seconds == 3.0


class TestRawSection:
    """The read side of a SHALLOW ``update_section``: what is stored, not what parses.

    A caller rewriting a whole sub-map has to start from the stored section, or its write
    deletes the keys its own model cannot read (ADR-010, one layer above the models).
    """

    def test_returns_the_section_as_stored(self, tmp_path):
        repo = ConfigRepository(ConfigLoader(_tmp_config(tmp_path)))
        repo.update_section("typed_accuracy", {"threshold": 0.42})

        assert repo.raw_section("typed_accuracy")["threshold"] == 0.42

    def test_keeps_what_the_typed_read_would_refuse(self, tmp_path):
        # A value of the WRONG TYPE makes feature_settings raise for the whole section; the
        # raw read still hands it back, which is what keeps a save from deleting it.
        repo = ConfigRepository(ConfigLoader(_tmp_config(tmp_path)))
        repo.update_section("overdue_guard", {"min_days": "soon"})

        with pytest.raises(ValidationError):
            repo.feature_settings("overdue_guard")
        assert repo.raw_section("overdue_guard")["min_days"] == "soon"

    def test_an_absent_section_is_an_empty_dict(self, config_repo):
        assert config_repo.raw_section("not_a_plugin") == {}

    def test_the_result_is_a_copy(self, tmp_path):
        repo = ConfigRepository(ConfigLoader(_tmp_config(tmp_path)))
        repo.update_section("auto_flip", {"per_deck": {"1": {"enabled": False}}})

        section = repo.raw_section("auto_flip")
        section["per_deck"]["1"]["enabled"] = True

        assert repo.raw_section("auto_flip")["per_deck"]["1"]["enabled"] is False


class TestProviderConfigWrites:
    """The Account dialog's writes: default-model picker + Keys subtab secret edits."""

    def test_set_active_llm_sets_provider_and_text_model(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_active_llm("gemini", text_model="gemini-3.5-flash")
        assert repo.config.llm.provider == "gemini"
        assert repo.config.llm.gemini.text_model == "gemini-3.5-flash"
        # Persisted: a fresh repo reads it back from providers.toml.
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert fresh.config.llm.provider == "gemini"
        assert fresh.config.llm.gemini.text_model == "gemini-3.5-flash"

    def test_set_active_llm_preserves_other_credentials(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_provider_secret("llm", "gemini", "api_key", "keep-me")
        repo.set_active_llm("gemini", text_model="m2")
        assert repo.config.llm.gemini.api_key == "keep-me"
        assert repo.config.llm.gemini.text_model == "m2"

    def test_set_active_llm_image_model_only(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_active_llm("openrouter", image_model="openai/gpt-image-1")
        assert repo.config.llm.provider == "openrouter"
        assert repo.config.llm.openrouter.image_model == "openai/gpt-image-1"

    def test_set_active_tts_sets_voice_for_supported_provider(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_active_tts("edge_tts", voice="vi-VN-HoaiMyNeural")
        assert repo.config.tts.provider == "edge_tts"
        assert repo.config.tts.edge_tts.voice == "vi-VN-HoaiMyNeural"

    def test_set_auto_voice_writes_language_mapping(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_auto_voice("ja", "edge_tts:ja-JP-NanamiNeural")
        assert repo.config.tts.auto_voices["ja"] == "edge_tts:ja-JP-NanamiNeural"
        # Persisted under [tts.auto_voices].ja — a fresh repo reads it back.
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert fresh.config.tts.auto_voices["ja"] == "edge_tts:ja-JP-NanamiNeural"

    def test_set_auto_voice_empty_value_deletes_the_entry(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_auto_voice("ja", "edge_tts:ja-JP-NanamiNeural")
        repo.set_auto_voice("ja", "")
        assert "ja" not in repo.config.tts.auto_voices
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert "ja" not in fresh.config.tts.auto_voices

    def test_set_active_tts_writes_piper_voice_to_its_model_field(self, tmp_path):
        # piper has no `voice` field — its selectable "voice" is the .onnx model, so the
        # value is stored as `model` (otherwise it would silently revert in the picker).
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_active_tts("piper", voice="vi_VN-vais1000-medium")
        assert repo.config.tts.provider == "piper"
        assert repo.config.tts.piper.model == "vi_VN-vais1000-medium"

    def test_set_active_tts_skips_voice_for_voiceless_provider(self, tmp_path):
        # google_translate has no `voice` field; writing one would persist a key the provider
        # never reads. The provider must still switch, and the config must reload cleanly.
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_active_tts("google_translate", voice="ignored")
        assert repo.config.tts.provider == "google_translate"
        assert repo.config.tts.google_translate.lang  # reloaded fine

    def test_set_provider_secret_persists_nested_field(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        repo.set_provider_secret("llm", "gemini_vertex", "project", "my-proj")
        assert repo.config.llm.gemini_vertex.project == "my-proj"
        fresh = ConfigRepository(ConfigLoader(tmp_cfg))
        assert fresh.config.llm.gemini_vertex.project == "my-proj"

    def test_set_provider_secret_rejects_unknown_domain(self, tmp_path):
        tmp_cfg = _tmp_config(tmp_path)
        repo = ConfigRepository(ConfigLoader(tmp_cfg))
        with pytest.raises(ValueError):
            repo.set_provider_secret("nope", "gemini", "api_key", "x")


class TestSecretsOutOfConfig:
    """Credentials are stored as references; the TOML never holds the raw secret."""

    def _repo(self, tmp_path):
        from omnia.core.config.secrets import SecretsStore

        tmp_cfg = _tmp_config(tmp_path)
        store = SecretsStore(tmp_path / "secrets")
        return ConfigRepository(ConfigLoader(tmp_cfg), store), tmp_cfg

    def test_secret_field_is_stored_as_reference_not_raw(self, tmp_path):
        repo, tmp_cfg = self._repo(tmp_path)
        repo.set_provider_fields("llm", "gemini", [("api_key", "secret", "AIza-XYZ")])
        # On disk the TOML holds only a reference; the raw key is nowhere in the file.
        raw = (tmp_cfg / "providers.toml").read_bytes()
        assert b"AIza-XYZ" not in raw
        on_disk = tomllib.loads(raw.decode())["llm"]["gemini"]["api_key"]
        assert on_disk.startswith("secret:")
        # But the loaded config resolves it back to the real key for the providers.
        assert repo.config.llm.gemini.api_key == "AIza-XYZ"

    def test_secret_resolves_on_a_fresh_repo(self, tmp_path):
        from omnia.core.config.secrets import SecretsStore

        repo, tmp_cfg = self._repo(tmp_path)
        repo.set_provider_fields(
            "llm", "openrouter", [("api_key", "secret", "sk-or-123")]
        )
        fresh = ConfigRepository(
            ConfigLoader(tmp_cfg), SecretsStore(tmp_path / "secrets")
        )
        assert fresh.config.llm.openrouter.api_key == "sk-or-123"

    def test_non_secret_field_stays_inline(self, tmp_path):
        repo, tmp_cfg = self._repo(tmp_path)
        repo.set_provider_fields(
            "llm", "gemini_vertex", [("project", "text", "my-proj")]
        )
        on_disk = tomllib.loads((tmp_cfg / "providers.toml").read_text())
        assert on_disk["llm"]["gemini_vertex"]["project"] == "my-proj"

    def test_file_kind_is_skipped_by_set_provider_fields(self, tmp_path):
        repo, _ = self._repo(tmp_path)
        # A "file" field is imported via Browse, never written by the batch save.
        repo.set_provider_fields(
            "llm", "gemini_vertex", [("credentials_path", "file", "/some/path")]
        )
        assert repo.config.llm.gemini_vertex.credentials_path == ""

    def test_credential_file_imported_and_resolves_to_path(self, tmp_path):
        src = tmp_path / "sa.json"
        src.write_text('{"type":"service_account"}')
        repo, _ = self._repo(tmp_path)
        resolved = repo.set_provider_credential_file(
            "llm", "gemini_vertex", "credentials_path", str(src)
        )
        # as_posix(): the repository returns a native path (backslashes on Windows).
        assert (
            Path(resolved)
            .as_posix()
            .endswith("secrets/llm.gemini_vertex.credentials_path.json")
        )
        # The loaded config resolves the ref to the absolute path of the secrets copy.
        assert repo.config.llm.gemini_vertex.credentials_path == resolved

    def test_clearing_a_secret_writes_empty(self, tmp_path):
        repo, _ = self._repo(tmp_path)
        repo.set_provider_fields("llm", "gemini", [("api_key", "secret", "k")])
        repo.set_provider_fields("llm", "gemini", [("api_key", "secret", "")])
        assert repo.config.llm.gemini.api_key == ""

    def test_same_field_name_across_domains_no_collision(self, tmp_path):
        # llm.openai.api_key and tts.openai.api_key must not share a secrets file.
        repo, _ = self._repo(tmp_path)
        repo.set_provider_fields("llm", "openai", [("api_key", "secret", "llm-key")])
        repo.set_provider_fields("tts", "openai", [("api_key", "secret", "tts-key")])
        assert repo.config.llm.openai.api_key == "llm-key"
        assert repo.config.tts.openai.api_key == "tts-key"


class TestConfigModels:
    def test_model_validation_rejects_bad_pass_ease(self):
        with pytest.raises(ValidationError):
            TypedAccuracySettings(pass_ease="medium")

    def test_model_validation_rejects_out_of_range_threshold(self):
        with pytest.raises(ValidationError):
            TypedAccuracySettings(threshold=1.5)

    def test_omnia_config_keeps_unknown_top_level_key(self):
        cfg = OmniaConfig.parse_obj({"unknown_future_key": 123, "log_level": "DEBUG"})
        assert cfg.log_level == "DEBUG"
        assert cfg.dict()["unknown_future_key"] == 123


class TestPersistedModelForwardCompat:
    """Every model that reaches persisted config must tolerate a NEWER Omnia's keys.

    ``omnia.toml``/``features.toml`` live in the synced collection config, so each device's
    Omnia — possibly a different release — loads and rewrites the same blob. A model that
    rejected unknown keys would brick config load (core) or silently fail to enable the feature
    (plugins) on whichever device is older; one that ignored them would erase the newer
    device's settings on the next write. Hence ``PersistedModel`` (``extra = "allow"``).
    """

    def test_every_registered_plugin_config_model_keeps_unknown_keys(self):
        from omnia.core.registry import get_registered

        models = {
            plugin_id: cls.config_model
            for plugin_id, cls in get_registered().items()
            if cls.config_model is not None
        }
        assert (
            models
        ), "no plugin declares a config_model — the loop would prove nothing"
        for plugin_id, model_cls in models.items():
            settings = model_cls.parse_obj({"from_a_newer_omnia": {"nested": 1}})
            assert settings.dict()["from_a_newer_omnia"] == {"nested": 1}, plugin_id

    def test_provider_settings_keep_unknown_keys(self):
        # [llm]/[tts] are persisted too (providers.toml), and a downgraded add-on must not
        # fail its whole config load over a subsection key it does not know.
        cfg = OmniaConfig.parse_obj(
            {"llm": {"provider": "gemini", "gemini": {"thinking_budget": 2048}}}
        )
        assert cfg.llm.gemini.dict()["thinking_budget"] == 2048
        assert cfg.llm.active() is cfg.llm.gemini

    def test_unknown_provider_subsection_loads_and_round_trips(self):
        """A whole ``[llm.<provider>]``/``[tts.<provider>]`` this release has never heard of.

        ``extra = "allow"`` keeps such a subsection as a raw dict, so config load survives a
        provider a NEWER Omnia added. It must then degrade, not crash: ``active()`` returns
        None (its ``isinstance(sub, BaseModel)`` guard rejects the raw dict, so the factory
        raises the clear "unknown provider" error lazily instead of the config bricking), and
        the subsection is handed back verbatim on the next write.
        """
        raw = {
            "llm": {"provider": "future_llm", "future_llm": {"api_key": "k"}},
            "tts": {"provider": "future_tts", "future_tts": {"voice": "v"}},
        }

        cfg = OmniaConfig.parse_obj(raw)

        assert (cfg.llm.provider, cfg.tts.provider) == ("future_llm", "future_tts")
        assert cfg.llm.active() is None
        assert cfg.tts.active() is None
        dumped = cfg.dict()
        assert dumped["llm"]["future_llm"] == {"api_key": "k"}
        assert dumped["tts"]["future_tts"] == {"voice": "v"}
        # …and it survives the full load → save → load cycle, not just one dump.
        assert OmniaConfig.parse_obj(dumped).dict()["llm"]["future_llm"] == {
            "api_key": "k"
        }

    def test_payload_models_still_reject_unknown_keys(self):
        # The other half of the seam: a model that never reaches storage stays strict.
        from omnia.core.config.base import StrictModel

        class Payload(StrictModel):
            value: int = 0

        with pytest.raises(ValidationError):
            Payload(value=1, typo=2)


class TestConfigLoader:
    def test_deep_merge_is_recursive(self):
        base = {"a": {"x": 1, "y": 2}, "b": 1}
        override = {"a": {"y": 3, "z": 4}, "c": 5}
        merged = deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}
        assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # base untouched


class TestTomlConfigLoaderTemplateDir:
    """The shipped ``*.example.toml`` templates and the LIVE files can live in separate dirs.

    This is what lets the live config move under ``user_files/config`` (preserved across add-on
    updates) while the templates stay at the add-on root ``config/`` (refreshed each update).
    """

    def test_seeds_live_files_from_separate_template_dir(self, tmp_path):
        from omnia.core.config.loader import TomlConfigLoader

        template_dir = tmp_path / "templates"
        live_dir = tmp_path / "live"
        template_dir.mkdir()
        (template_dir / "omnia.example.toml").write_text(
            'log_level = "DEBUG"\n', encoding="utf-8"
        )
        (template_dir / "features.example.toml").write_text(
            "[auto_flip]\ndelay_question_seconds = 4.0\n", encoding="utf-8"
        )
        (template_dir / "providers.example.toml").write_text(
            '[llm]\nprovider = "gemini"\n', encoding="utf-8"
        )

        loader = TomlConfigLoader(live_dir, template_dir=template_dir)
        loader.ensure_live_files()

        # The live files are created in the LIVE dir (created on demand), from the templates.
        for name in ("omnia.toml", "features.toml", "providers.toml"):
            assert (live_dir / name).exists()
            assert not (template_dir / name).exists()
        # Reads come from the live dir; writes stay there, never the template dir.
        assert loader.read_file("omnia.toml") == {"log_level": "DEBUG"}
        loader.write_file("omnia.toml", {"log_level": "WARNING"})
        assert loader.read_file("omnia.toml") == {"log_level": "WARNING"}
        assert not (template_dir / "omnia.toml").exists()

    def test_template_dir_defaults_to_config_dir(self, tmp_path):
        # Single-dir back-compat: with no template_dir, templates + live files share one dir.
        from omnia.core.config.loader import TomlConfigLoader

        (tmp_path / "omnia.example.toml").write_text(
            'log_level = "INFO"\n', encoding="utf-8"
        )
        loader = TomlConfigLoader(tmp_path)
        loader.ensure_live_files()
        assert (tmp_path / "omnia.toml").exists()
        assert loader.read_file("omnia.toml") == {"log_level": "INFO"}


def _tmp_config(tmp_path):
    """Seed ``tmp_path`` from the tracked ``*.example.toml`` templates and return it.

    Gives each test an isolated config directory; the loader's ``ensure_live_files`` then
    creates the live files from these templates, so no real credentials are involved.
    """
    import shutil
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "src" / "omnia" / "config"
    for template in src.glob("*.example.toml"):
        shutil.copy(template, tmp_path / template.name)
    return tmp_path
