"""Typed configuration layer (Pydantic v1 + TOML).

Config is split into three domains — ``omnia`` (``log_level`` + ``[plugins.*]``), ``features``
(per-feature sections), and ``providers`` (``[llm]`` with one subsection per provider +
``[tts]``) — behind a swappable storage backend (ADR-006). The DEFAULT backend keeps ``omnia``/
``features`` in the Anki collection (``col.get_config``/``set_config``, synced) via
:class:`CollectionConfigLoader`; the ``toml`` backend keeps all three in live ``*.toml`` files
via :class:`TomlConfigLoader` (both implement :class:`BaseConfigLoader`; the runtime choice is
the ``PersistenceDispatcher``'s, from ``OMNIA_CONFIG_STORAGE``). ``providers.toml`` always stays
on disk under the live config dir (credentials must never enter a synced collection), seeded on
first run from the tracked ``*.example.toml`` templates; credential files live under
``.secrets/``. The :class:`ConfigRepository` is the read/write facade plugins use; it merges +
validates the CORE sections into a typed :class:`OmniaConfig`. Per-feature settings are owned by
each plugin (``plugins/<plugin>/config.py``) and resolved via the registry by
:meth:`ConfigRepository.feature_settings` — so this core package never imports ``plugins/*``.
"""

from __future__ import annotations

from omnia.core.config.loader import (
    BaseConfigLoader,
    CollectionConfigLoader,
    ConfigLoader,
    TomlConfigLoader,
    build_config_loader,
)
from omnia.core.config.models import (
    LLMSettings,
    OmniaConfig,
    TTSSettings,
)
from omnia.core.config.repository import ConfigRepository
from omnia.core.config.schema import schema_from_model
from omnia.core.config.secrets import SecretsStore

__all__ = [
    "BaseConfigLoader",
    "CollectionConfigLoader",
    "ConfigLoader",
    "ConfigRepository",
    "LLMSettings",
    "OmniaConfig",
    "SecretsStore",
    "TTSSettings",
    "TomlConfigLoader",
    "build_config_loader",
    "schema_from_model",
]
