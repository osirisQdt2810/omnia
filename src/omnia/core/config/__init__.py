"""Typed configuration layer (Pydantic v1 + TOML).

Defaults ship in ``omnia/config/`` (``omnia.toml`` high-level, ``features.toml`` /
``providers.toml`` per domain — the LLM block holds one subsection per provider); user
overrides persist to ``user_files/omnia.toml``. The :class:`ConfigLoader` merges + validates
the CORE sections into a typed :class:`OmniaConfig`, and the :class:`ConfigRepository` is the
read/write facade plugins use. Per-feature settings are owned by each plugin
(``plugins/<plugin>/config.py``) and resolved via the registry by
:meth:`ConfigRepository.feature_settings` — so this core package never imports ``plugins/*``.
"""

from __future__ import annotations

from omnia.core.config.loader import ConfigLoader
from omnia.core.config.models import (
    LLMSettings,
    OmniaConfig,
    TTSSettings,
)
from omnia.core.config.repository import ConfigRepository
from omnia.core.config.schema import schema_from_model

__all__ = [
    "ConfigLoader",
    "ConfigRepository",
    "LLMSettings",
    "OmniaConfig",
    "TTSSettings",
    "schema_from_model",
]
