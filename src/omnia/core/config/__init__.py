"""Typed configuration layer (Pydantic v2 + TOML).

Defaults ship in ``omnia/config/`` (``omnia.toml`` high-level, ``features.toml`` /
``providers.toml`` per domain — the LLM block holds one subsection per provider); user
overrides persist to ``user_files/omnia.toml``. The :class:`ConfigLoader` merges + validates
them into a typed :class:`OmniaConfig`, and the :class:`ConfigRepository` is the read/write
facade plugins use.
"""

from __future__ import annotations

from omnia.core.config.loader import ConfigLoader
from omnia.core.config.models import (
    AutoFlipSettings,
    DisplayIntervalSettings,
    LLMSettings,
    OmniaConfig,
    OverdueGuardSettings,
    SmartNotesSettings,
    TTSSettings,
    TypedAccuracySettings,
)
from omnia.core.config.repository import ConfigRepository

__all__ = [
    "AutoFlipSettings",
    "ConfigLoader",
    "ConfigRepository",
    "DisplayIntervalSettings",
    "LLMSettings",
    "OmniaConfig",
    "OverdueGuardSettings",
    "SmartNotesSettings",
    "TTSSettings",
    "TypedAccuracySettings",
]
