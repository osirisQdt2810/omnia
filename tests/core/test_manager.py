"""Tests for the plugin lifecycle manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnia.core import registry
from omnia.core.config import ConfigLoader, ConfigRepository
from omnia.core.manager import PluginManager
from omnia.core.plugin import AddonPaths, FeaturePlugin

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "omnia" / "config"


@pytest.fixture(autouse=True)
def clean_registry():
    snapshot = dict(registry.FEATURE_REGISTRY)
    registry.FEATURE_REGISTRY.clear()
    yield
    registry.FEATURE_REGISTRY.clear()
    registry.FEATURE_REGISTRY.update(snapshot)


@pytest.fixture
def make_manager(tmp_path):
    """Factory: build a PluginManager over a fresh repository (isolated tmp config dir)."""
    import shutil

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for template in _CONFIG_DIR.glob("*.example.toml"):
        shutil.copy(template, cfg_dir / template.name)

    def _make():
        repo = ConfigRepository(ConfigLoader(cfg_dir))
        paths = AddonPaths(tmp_path, tmp_path / "web", tmp_path / "uf")
        return PluginManager(repo, paths), repo

    return _make


class TestPluginManager:
    def test_setup_activates_enabled_plugins_only(self, make_manager):
        events = []

        @registry.register("on_plugin")
        class On(FeaturePlugin):
            def on_enable(self, ctx):
                events.append(("enable", "on_plugin"))
                ctx.ease.add_transformer(self.id, lambda c, e: 2)

            def on_disable(self, ctx):
                ctx.ease.remove_transformer(self.id)

        @registry.register("off_plugin")
        class Off(FeaturePlugin):
            def on_enable(self, ctx):
                events.append(("enable", "off_plugin"))

            def on_disable(self, ctx):
                pass

        mgr, repo = make_manager()
        repo.set_enabled("on_plugin", True)
        try:
            mgr.setup()
            assert mgr.is_active("on_plugin") is True
            assert mgr.is_active("off_plugin") is False
            assert ("enable", "on_plugin") in events
            assert ("enable", "off_plugin") not in events
        finally:
            mgr.teardown()

    def test_runtime_toggle_enable_disable(self, make_manager):
        @registry.register("toggle")
        class Toggle(FeaturePlugin):
            def on_enable(self, ctx):
                ctx.ease.add_transformer(self.id, lambda c, e: 2)

            def on_disable(self, ctx):
                ctx.ease.remove_transformer(self.id)

        mgr, repo = make_manager()
        try:
            mgr.setup()
            assert mgr.is_active("toggle") is False
            assert mgr.set_enabled("toggle", True) is True
            assert mgr.is_active("toggle") is True
            assert repo.is_enabled("toggle") is True
            assert mgr.set_enabled("toggle", False) is False
            assert mgr.is_active("toggle") is False
            assert repo.is_enabled("toggle") is False
        finally:
            mgr.teardown()

    def test_enable_failure_is_isolated(self, make_manager):
        @registry.register("bad")
        class Bad(FeaturePlugin):
            def on_enable(self, ctx):
                raise RuntimeError("boom")

            def on_disable(self, ctx):
                pass

        mgr, repo = make_manager()
        try:
            mgr.setup()
            assert mgr.set_enabled("bad", True) is False
            assert mgr.is_active("bad") is False
            assert repo.is_enabled("bad") is True  # intent persisted for next session
        finally:
            mgr.teardown()

    def test_typed_settings_reach_the_plugin(self, make_manager):
        from omnia.plugins.auto_flip.config import AutoFlipSettings

        seen = {}

        @registry.register("auto_flip")  # real config id -> typed settings model
        class Fake(FeaturePlugin):
            # The plugin declares its own settings model; the repository resolves it via the
            # registry to validate the raw [auto_flip] namespace into a typed instance.
            config_model = AutoFlipSettings

            def on_enable(self, ctx):
                seen["delay"] = ctx.settings.delay_question_seconds

            def on_disable(self, ctx):
                pass

        mgr, repo = make_manager()
        repo.set_enabled("auto_flip", True)
        try:
            mgr.setup()
            assert seen["delay"] == 3.0  # from config/features.toml default
        finally:
            mgr.teardown()

    def test_unknown_plugin_raises(self, make_manager):
        mgr, _ = make_manager()
        mgr.setup()
        try:
            with pytest.raises(KeyError):
                mgr.set_enabled("nope", True)
        finally:
            mgr.teardown()
