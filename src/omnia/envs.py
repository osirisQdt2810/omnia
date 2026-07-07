"""Project-wide, environment-overridable knobs (vLLM/vio-ai-style lazy env access).

Read a knob as ``envs.NAME`` (e.g. ``envs.OMNIA_LLM_TEMPERATURE``). Each read pulls the
*current* environment value — so tests can ``monkeypatch.setenv`` at runtime — and falls back
to the default; a malformed value never raises, it just yields the default.

These are small runtime toggles/overrides, not the primary config: structured, user-facing
settings (providers, models, per-provider ``temperature``, secrets) live in the ``config/*.toml``
files behind :class:`~omnia.core.config.repository.ConfigRepository`. An env knob here is the
escape hatch — handy for tests (deterministic ``temperature=0``), CI, and power users — and,
where it overlaps a config field, the **env wins** so a one-off override needs no file edit.

Add a knob as a new entry in ``environment_variables`` (keyed by the env var name).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def _bool(name: str, default: bool = False) -> bool:
    """Truthy env (``1/true/yes/on``) → True; unset → ``default`` (never raises)."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    """``float(env)`` if parseable, else ``default`` (never raises)."""
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


environment_variables: dict[str, Callable[[], Any]] = {
    # ── logging ── empty -> use the configured log_level (config/omnia.toml).
    "OMNIA_LOG_LEVEL": lambda: _str("OMNIA_LOG_LEVEL", ""),
    # ── per-LLM-call temperatures (OMNIA_{PLUGIN}_{FUNCTION}_TEMPERATURE) ──
    # Each distinct LLM call gets its own knob, defaulted to what that task wants and
    # env-overridable (e.g. set to 0 for deterministic runs). The general per-provider default
    # temperature (used by smart_notes FIELD GENERATION) lives in providers.toml, not here —
    # these override only the specific structured/authoring calls below.
    #   smart_notes · detect-language: classification → deterministic.
    "OMNIA_SMART_NOTES_DETECT_LANGUAGE_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_DETECT_LANGUAGE_TEMPERATURE", 0.0
    ),
    #   smart_notes · auto-prompt: infer each field's type+prompt from its name.
    "OMNIA_SMART_NOTES_AUTO_PROMPT_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_AUTO_PROMPT_TEMPERATURE", 0.4
    ),
    #   smart_notes · improve-prompt: polish one field's rough prompt.
    "OMNIA_SMART_NOTES_IMPROVE_PROMPT_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_IMPROVE_PROMPT_TEMPERATURE", 0.4
    ),
    #   smart_notes · improve-all: polish many fields' prompts at once.
    "OMNIA_SMART_NOTES_IMPROVE_ALL_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_IMPROVE_ALL_TEMPERATURE", 0.4
    ),
    #   smart_notes · classify-deps: label refs hard/soft → deterministic (B2: no flicker).
    "OMNIA_SMART_NOTES_CLASSIFY_DEPS_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_CLASSIFY_DEPS_TEMPERATURE", 0.0
    ),
    #   smart_notes · rewrite-edge: rewrite a prompt to reflect ONE graph edge change.
    "OMNIA_SMART_NOTES_REWRITE_EDGE_TEMPERATURE": lambda: _float(
        "OMNIA_SMART_NOTES_REWRITE_EDGE_TEMPERATURE", 0.3
    ),
    # ── HTTP ── default request timeout (seconds) for the stdlib HTTP client.
    "OMNIA_HTTP_TIMEOUT": lambda: _float("OMNIA_HTTP_TIMEOUT", 30.0),
    # ── storage dispatch (ADR-006) ── one knob per persistence concern, selecting its backend.
    # Default "database" = the Anki collection (config/voices in col config, usage in a col.db
    # table); the file backends stay first-class and selectable. These are read at startup by
    # the PersistenceDispatcher: changing a value triggers a ONE-TIME sync of that concern's
    # data from the previous backend to the newly-selected one on the next startup (the last-used
    # value is remembered in user_files/.storage.json), so switching never loses state.
    "OMNIA_CONFIG_STORAGE": lambda: _str(
        "OMNIA_CONFIG_STORAGE", "database"
    ),  # "database" | "toml"
    "OMNIA_USAGE_STORAGE": lambda: _str(
        "OMNIA_USAGE_STORAGE", "database"
    ),  # "database" | "json"
    "OMNIA_VOICE_CACHE_STORAGE": lambda: _str(
        "OMNIA_VOICE_CACHE_STORAGE", "database"
    ),  # "database" | "json"
}


def __getattr__(name: str) -> Any:
    # Lazy evaluation of environment variables (PEP 562) — read at access time, not import.
    if name in environment_variables:
        return environment_variables[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(environment_variables)
