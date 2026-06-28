"""The plugin lifecycle manager (ADR-002).

Built once at add-on startup. It installs the shared reviewer seams, instantiates every
registered :class:`FeaturePlugin`, and drives ``on_enable``/``on_disable`` based on the
config's ``enabled`` map. The settings GUI calls :meth:`set_enabled` to toggle a feature at
runtime.

A failure in one plugin's enable/disable is isolated (logged, not re-raised) so a single
bad feature can't take down Anki or the other features — this is the plugin-isolation
boundary referenced in CONVENTIONS Part 1 (Error Handling).
"""

from __future__ import annotations

from typing import Optional

from omnia.core.config import ConfigRepository
from omnia.core.logging import get_logger
from omnia.core.plugin import AddonPaths, FeaturePlugin, PluginContext
from omnia.core.providers import ProviderHub
from omnia.core.registry import get_registered
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.core.reviewer.web_injector import WebInjector


class PluginManager:
    """Owns plugin instances, their contexts, and the active set."""

    def __init__(
        self,
        config: ConfigRepository,
        paths: AddonPaths,
        *,
        providers: Optional[ProviderHub] = None,
        ease: Optional[EasePipeline] = None,
        web: Optional[WebInjector] = None,
    ) -> None:
        self._config = config
        self._paths = paths
        self._providers = providers or ProviderHub(
            config.llm_settings(), config.tts_settings()
        )
        self._ease = ease or EasePipeline()
        self._web = web or WebInjector()
        self._log = get_logger()
        self._plugins: dict[str, FeaturePlugin] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._active: set[str] = set()

    # --- startup --------------------------------------------------------------------
    def setup(self) -> None:
        """Install the seams, instantiate plugins, and enable those marked enabled."""
        self._ease.install()
        self._web.install()
        for plugin_id, cls in get_registered().items():
            self._plugins[plugin_id] = cls()
        for plugin_id in self._plugins:
            if self._config.is_enabled(plugin_id):
                self._activate(plugin_id)

    # --- context ---------------------------------------------------------------------
    def _context(self, plugin_id: str) -> PluginContext:
        ctx = self._contexts.get(plugin_id)
        if ctx is None:
            ctx = PluginContext(
                plugin_id=plugin_id,
                settings=self._config.feature_settings(plugin_id),
                log=get_logger(plugin_id),
                ease=self._ease,
                web=self._web,
                providers=self._providers,
                paths=self._paths,
            )
            self._contexts[plugin_id] = ctx
        return ctx

    # --- activation (isolated) ------------------------------------------------------
    def _activate(self, plugin_id: str) -> bool:
        plugin = self._plugins[plugin_id]
        try:
            plugin.on_enable(self._context(plugin_id))
        except Exception:  # plugin-isolation boundary — see module docstring
            self._log.exception("Failed to enable plugin %s", plugin_id)
            return False
        self._active.add(plugin_id)
        self._log.info("Enabled plugin %s", plugin_id)
        return True

    def _deactivate(self, plugin_id: str) -> bool:
        plugin = self._plugins[plugin_id]
        ok = True
        try:
            plugin.on_disable(self._context(plugin_id))
        except Exception:  # plugin-isolation boundary — see module docstring
            self._log.exception("Failed to disable plugin %s", plugin_id)
            ok = False
        # Always drop from the active set: a plugin that failed teardown is in an unknown
        # state, and leaving it "active" only causes cascading errors on retry/teardown.
        self._active.discard(plugin_id)
        if ok:
            self._log.info("Disabled plugin %s", plugin_id)
        return ok

    # --- public API (used by the settings GUI) --------------------------------------
    def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        """Toggle a plugin at runtime; persist the choice. Returns the resulting active state.

        Persists the ``enabled`` flag regardless, so the user's intent survives a restart
        even if activation failed this session.
        """
        if plugin_id not in self._plugins:
            raise KeyError(f"Unknown plugin: {plugin_id!r}")
        self._config.set_enabled(plugin_id, enabled)  # persists + reloads config
        if enabled and plugin_id not in self._active:
            return self._activate(plugin_id)
        if not enabled and plugin_id in self._active:
            self._deactivate(plugin_id)
            return False
        return plugin_id in self._active

    def reload(self, plugin_id: str) -> None:
        """Re-apply a plugin's config by disabling then re-enabling it (if active).

        Drops the cached context so the plugin is rebuilt with a fresh settings snapshot.
        """
        if plugin_id in self._active:
            self._deactivate(plugin_id)
            self._contexts.pop(plugin_id, None)
            self._activate(plugin_id)

    def plugins(self) -> list[FeaturePlugin]:
        """Return plugin instances sorted for display (by ``order`` then ``name``)."""
        return sorted(self._plugins.values(), key=lambda p: (p.order, p.name))

    def is_active(self, plugin_id: str) -> bool:
        """Return whether ``plugin_id`` is currently active this session."""
        return plugin_id in self._active

    @property
    def config(self) -> ConfigRepository:
        """The config repository (used by the settings GUI)."""
        return self._config

    def teardown(self) -> None:
        """Disable all active plugins and uninstall the seams (on profile close)."""
        for plugin_id in list(self._active):
            self._deactivate(plugin_id)
        self._ease.uninstall()
        self._web.uninstall()
