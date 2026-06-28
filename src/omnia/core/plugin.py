"""The feature-plugin contract: :class:`FeaturePlugin`, :class:`PluginContext`, and the
:class:`ConfigField` descriptor the settings GUI renders generically.

Import-light by design: no ``aqt``/``anki`` at runtime. Seam types are referenced only
under ``TYPE_CHECKING`` so feature logic stays unit-testable headless.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import logging

    from pydantic import BaseModel

    from omnia.core.providers import ProviderHub
    from omnia.core.reviewer.ease_pipeline import EasePipeline
    from omnia.core.reviewer.web_injector import WebInjector


# Supported config-field kinds the settings GUI knows how to render.
FIELD_KINDS = ("bool", "int", "float", "text", "secret", "choice")


@dataclass(frozen=True)
class ConfigField:
    """Declarative description of one configurable option, rendered by the settings GUI.

    Attributes:
        key: The settings key (also the JSON key under the plugin's namespace).
        label: Human-readable label.
        kind: One of :data:`FIELD_KINDS`.
        default: Default value if the key is absent.
        help: Optional one-line explanation shown under the control.
        choices: Allowed values when ``kind == "choice"``.
        minimum / maximum: Bounds for numeric kinds.
    """

    key: str
    label: str
    kind: str
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"Unknown config field kind: {self.kind!r}")


@dataclass
class AddonPaths:
    """Filesystem locations the add-on uses at runtime."""

    addon_dir: Path
    web_dir: Path
    user_files_dir: Path


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin needs, injected on enable. No globals; no reaching into ``mw``.

    A plugin uses the shared seams here (``ease``, ``web``, ``providers``) instead of
    patching Anki directly, and reads its typed ``settings`` model. Frozen so a plugin
    can't swap a seam reference; per-feature runtime state lives on the plugin instance
    (``self``), not here. ``settings`` is a snapshot taken at enable time — the manager
    rebuilds the context (via ``reload``) when config changes.
    """

    plugin_id: str
    settings: Optional[BaseModel]
    log: logging.Logger
    ease: EasePipeline
    web: WebInjector
    providers: ProviderHub
    paths: AddonPaths


class FeaturePlugin:
    """Base class for every Omnia feature.

    Subclasses set :attr:`name`/:attr:`description` (``id`` is stamped by ``@register``),
    implement :meth:`on_enable` / :meth:`on_disable`, and may declare options via
    :meth:`config_schema`. A plugin MUST fully tear down in :meth:`on_disable` — remove its
    ease transformers, web assets, pycmd handlers, hooks, and timers — so toggling it off at
    runtime leaves no trace.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    # Lower sorts earlier in the settings list.
    order: int = 100

    def on_enable(self, ctx: PluginContext) -> None:
        """Activate the feature. Register seams/hooks here. Called when ticked or at startup."""
        raise NotImplementedError

    def on_disable(self, ctx: PluginContext) -> None:
        """Fully deactivate the feature, removing everything :meth:`on_enable` added."""
        raise NotImplementedError

    def config_schema(self) -> list[ConfigField]:
        """Return the configurable options for the settings GUI (default: none)."""
        return []
