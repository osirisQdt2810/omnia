"""The read/write facade over the typed config.

Plugins and the settings GUI talk to a :class:`ConfigRepository`, never to the raw files.
The core sections (``log_level``/``plugins``/``llm``/``tts``) are validated into an
:class:`OmniaConfig`; the per-feature sections are validated lazily by each plugin's OWN
``config_model``, resolved through the registry. The repository keeps the raw merged dict so
it can hand a plugin's namespace to that model — which is how ``core`` stays decoupled from
``plugins`` (the registry holds plugin classes but lives in ``core``; plugins import IT).
Writes update the owning domain live file (``plugins``→``omnia.toml``, feature sections→
``features.toml``, ``llm``/``tts``→``providers.toml``) and re-validate; there is no override
layer.
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
    """Typed config access + persistence to the owning domain live files."""

    def __init__(self, loader: ConfigLoader) -> None:
        self._loader = loader
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
        """Persist the enabled flag for ``plugin_id`` (in ``omnia.toml``) and reload."""
        data = self._loader.read_file("omnia.toml")
        data.setdefault("plugins", {}).setdefault(plugin_id, {})["enabled"] = bool(
            enabled
        )
        self._loader.write_file("omnia.toml", data)
        self._reload()

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
        """Merge ``values`` into ``section`` in its owning live file and reload.

        ``section`` is a top-level key like ``"auto_flip"`` or ``"llm"``; the owning file is
        resolved by :meth:`_file_for`.
        """
        fname = self._file_for(section)
        data = self._loader.read_file(fname)
        data.setdefault(section, {}).update(values)
        self._loader.write_file(fname, data)
        self._reload()

    @staticmethod
    def _file_for(section: str) -> str:
        """Return the live file that owns ``section``."""
        if section in ("llm", "tts"):
            return "providers.toml"
        if section in ("log_level", "plugins"):
            return "omnia.toml"
        return "features.toml"

    def _reload(self) -> None:
        self._config = self._loader.load()
        self._merged = self._loader.load_merged()
