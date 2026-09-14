"""Tests for the plugin lifecycle manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnia.core import registry
from omnia.core.config import ConfigLoader, ConfigRepository
from omnia.core.manager import PluginManager, group_plugins, grouped_plugins
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

    def test_config_change_between_disable_and_enable_is_reflected(self, make_manager):
        # Regression (L1): disabling must evict the cached PluginContext so a config edit made
        # while the plugin is off is picked up on re-enable (not the stale prior snapshot).
        from omnia.plugins.auto_flip.config import AutoFlipSettings

        seen: list[float] = []

        @registry.register("auto_flip")
        class Fake(FeaturePlugin):
            config_model = AutoFlipSettings

            def on_enable(self, ctx):
                seen.append(ctx.settings.delay_question_seconds)

            def on_disable(self, ctx):
                pass

        mgr, repo = make_manager()
        try:
            mgr.setup()
            assert mgr.set_enabled("auto_flip", True) is True
            assert seen[-1] == 3.0  # default from the example config
            mgr.set_enabled("auto_flip", False)
            repo.update_section("auto_flip", {"delay_question_seconds": 9.0})
            assert mgr.set_enabled("auto_flip", True) is True
            assert seen[-1] == 9.0  # the edit reached the plugin via a fresh context
        finally:
            mgr.teardown()

    def test_plugin_still_enables_with_a_newer_omnias_config_key(self, make_manager):
        # features.toml syncs (ADR-006/ADR-008), so another device's NEWER Omnia can add a key
        # this version never heard of. feature_settings() runs inside _activate's try-block, so
        # a strict model would turn that key into "feature silently never enables".
        from omnia.plugins.auto_flip.config import AutoFlipSettings

        seen: list[float] = []

        @registry.register("auto_flip")
        class Fake(FeaturePlugin):
            config_model = AutoFlipSettings

            def on_enable(self, ctx):
                seen.append(ctx.settings.delay_question_seconds)

            def on_disable(self, ctx):
                pass

        mgr, repo = make_manager()
        repo.update_section(
            "auto_flip", {"delay_question_seconds": 4.0, "from_a_newer_omnia": True}
        )
        repo.set_enabled("auto_flip", True)
        try:
            mgr.setup()
            assert mgr.is_active("auto_flip") is True
            assert seen == [
                4.0
            ]  # the settings this version DOES understand still applied
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


class TestAPluginThatIsNotAChoice:
    """``always_on``: it runs whenever Anki does, and never appears in the grid.

    For a feature another program depends on — ``word_lookup`` serves the clippers — where a card
    would be a switch whose only effect is to break something else, and a user who found it off
    would experience it as the clippers being broken rather than as a setting they changed.
    """

    @pytest.fixture
    def plugins(self):
        @registry.register("service")
        class Service(FeaturePlugin):
            name = "A Service"
            always_on = True

            def on_enable(self, ctx):
                pass

            def on_disable(self, ctx):
                pass

        @registry.register("ordinary")
        class Ordinary(FeaturePlugin):
            name = "An Ordinary Feature"
            group = "Reviewing"

            def on_enable(self, ctx):
                pass

            def on_disable(self, ctx):
                pass

        return Service, Ordinary

    def test_it_is_activated_without_ever_being_enabled(self, make_manager, plugins):
        # The enable map has no entry for it and must not be consulted: a stale ``enabled =
        # false`` left by an older build would otherwise switch off what the clippers depend on.
        mgr, repo = make_manager()

        mgr.setup()
        try:
            assert mgr.is_active("service") is True
            assert repo.is_enabled("service") is False, "it wrote a flag nothing reads"
            assert mgr.is_active("ordinary") is False
        finally:
            mgr.teardown()

    def test_it_is_left_out_of_the_grid(self, make_manager, plugins):
        mgr, _repo = make_manager()
        mgr.setup()
        try:
            groups = grouped_plugins(mgr)
        finally:
            mgr.teardown()

        assert [name for name, _ in groups] == ["Reviewing"]
        shown = {p.id for _name, members in groups for p in members}
        assert shown == {"ordinary"}

    def test_a_group_holding_only_always_on_plugins_does_not_render(self, plugins):
        # Not an empty tile with a zero count — the section simply is not there.
        service, _ordinary = plugins

        assert group_plugins([service()]) == []

    def test_it_can_still_be_found_by_id(self, make_manager, plugins):
        # Hiding it from the GRID must not hide it from the code that looks one up to reload or
        # configure it.
        mgr, _repo = make_manager()
        mgr.setup()
        try:
            assert {p.id for p in mgr.plugins()} == {"service", "ordinary"}
        finally:
            mgr.teardown()

    def test_toggling_it_writes_nothing_and_does_not_stop_it(
        self, make_manager, plugins
    ):
        # Nothing in the UI can reach this, but the API must not pretend to have acted: there is
        # no enable flag to set, and writing one leaves a value read by nobody.
        mgr, repo = make_manager()
        mgr.setup()
        try:
            still_active = mgr.set_enabled("service", False)

            assert still_active is True
            assert mgr.is_active("service") is True
            assert repo.is_enabled("service") is False
        finally:
            mgr.teardown()
