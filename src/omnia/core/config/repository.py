"""The read/write facade over the typed config.

Plugins and the settings GUI talk to a :class:`ConfigRepository`, never to the raw files.
The core sections (``log_level``/``plugins``/``llm``/``tts``) are validated into an
:class:`OmniaConfig`; the per-feature sections are validated lazily by each plugin's OWN
``config_model``, resolved through the registry. The repository keeps the raw merged dict so
it can hand a plugin's namespace to that model — which is how ``core`` stays decoupled from
``plugins`` (the registry holds plugin classes but lives in ``core``; plugins import IT).
Writes update the user override layer (``user_files/omnia.toml``) and re-validate.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from omnia.core.config.loader import ConfigLoader
from omnia.core.config.models import (
    LLMSettings,
    OmniaConfig,
    TTSSettings,
)
from omnia.core.registry import get_registered


class ConfigRepository:
    """Typed config access + persistence of user overrides."""

    def __init__(self, loader: ConfigLoader) -> None:
        self._loader = loader
        self._overrides: dict[str, Any] = loader.read_overrides()
        self._config: OmniaConfig = loader.load()
        # Retained so a plugin's namespace can be validated by its own config_model.
        self._merged: dict[str, Any] = loader.load_merged()

    @property
    def config(self) -> OmniaConfig:
        """The current validated CORE configuration."""
        return self._config

    # --- enabled state --------------------------------------------------------------
    def is_enabled(self, plugin_id: str) -> bool:
        """Return whether ``plugin_id`` is enabled (default False)."""
        toggle = self._config.plugins.get(plugin_id)
        return bool(toggle and toggle.enabled)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        """Persist the enabled flag for ``plugin_id`` and reload."""
        section = self._overrides.setdefault("plugins", {}).setdefault(plugin_id, {})
        section["enabled"] = bool(enabled)
        self._persist()

    # --- typed settings access ------------------------------------------------------
    def feature_settings(self, plugin_id: str) -> Optional[BaseModel]:
        """Return the plugin's typed settings, parsed from its raw config namespace.

        Resolves the plugin's ``config_model`` via the registry and validates the merged
        ``[<plugin_id>]`` section against it. Returns None for an unregistered plugin or one
        that declares no ``config_model``. Keeping the lookup in the registry (which lives in
        ``core``) is what lets this stay coupling-clean: ``core/config`` never imports
        ``plugins/*``.
        """
        plugin_cls = get_registered().get(plugin_id)
        model_cls = getattr(plugin_cls, "config_model", None) if plugin_cls else None
        if model_cls is None:
            return None
        return model_cls.parse_obj(self._merged.get(plugin_id, {}))

    def llm_settings(self) -> LLMSettings:
        """Return the LLM provider settings."""
        return self._config.llm

    def tts_settings(self) -> TTSSettings:
        """Return the TTS provider settings."""
        return self._config.tts

    # --- writes (used by the settings GUI) ------------------------------------------
    def update_section(self, section: str, values: dict[str, Any]) -> None:
        """Merge ``values`` into the override layer under ``section`` and reload.

        ``section`` is a top-level key like ``"auto_flip"`` or ``"llm"``.
        """
        target = self._overrides.setdefault(section, {})
        target.update(values)
        self._persist()

    def _persist(self) -> None:
        self._loader.save_overrides(self._overrides)
        self._config = self._loader.load()
        self._merged = self._loader.load_merged()
