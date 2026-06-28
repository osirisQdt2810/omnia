"""The read/write facade over the typed config.

Plugins and the settings GUI talk to a :class:`ConfigRepository`, never to the raw files.
Reads return validated Pydantic models; writes update the user override layer
(``user_files/omnia.toml``) and re-validate. The repository is the one piece of config
state the rest of the add-on shares.
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


class ConfigRepository:
    """Typed config access + persistence of user overrides."""

    def __init__(self, loader: ConfigLoader) -> None:
        self._loader = loader
        self._overrides: dict[str, Any] = loader.read_overrides()
        self._config: OmniaConfig = loader.load()

    @property
    def config(self) -> OmniaConfig:
        """The current validated configuration."""
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
        """Return the typed settings model for ``plugin_id``, or None if it has none."""
        settings = getattr(self._config, plugin_id, None)
        return settings if isinstance(settings, BaseModel) else None

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
