"""Typed configuration layer (Pydantic v1 + TOML).

Config lives in three live domain files under the add-on's ``config/`` dir (``omnia.toml`` →
``log_level`` + ``[plugins.*]``, ``features.toml`` → per-feature sections, ``providers.toml``
→ ``[llm]`` with one subsection per provider + ``[tts]``); they are edited directly and
written back to, seeded on first run from the tracked ``*.example.toml`` templates (credential
files live under ``.secrets/``). There is no override layer. The :class:`ConfigLoader` merges
+ validates the CORE sections into a typed :class:`OmniaConfig`, and the
:class:`ConfigRepository` is the read/write facade plugins use. Per-feature settings are owned
by each plugin (``plugins/<plugin>/config.py``) and resolved via the registry by
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
