"""The feature-plugin contract: :class:`FeaturePlugin`, :class:`PluginContext`, and the
:class:`ConfigField` descriptor the settings GUI renders generically.

Import-light by design: no ``aqt``/``anki`` at runtime. Seam types are referenced only
under ``TYPE_CHECKING`` so feature logic stays unit-testable headless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    import logging

    from pydantic import BaseModel

    from omnia.core.config import ConfigRepository
    from omnia.core.providers import ProviderHub
    from omnia.core.reviewer.ease_pipeline import EasePipeline
    from omnia.core.reviewer.web_injector import WebInjector


# Supported config-field kinds the settings GUI knows how to render.
FIELD_KINDS = ("bool", "int", "float", "text", "secret", "choice", "color")


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
    # The config facade + a "reload me" callback, so a plugin can persist user choices made in
    # its OWN in-Anki UI (e.g. a deck-options menu) and re-apply itself with the new settings.
    config: ConfigRepository
    reload_self: Callable[[], None]


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
    # Which settings-UI section this feature belongs to (e.g. "Reviewing", "Grading", "AI").
    group: str = "General"
    # A longer hover hint for the settings UI; falls back to ``description`` when empty.
    tooltip: str = ""
    # Lower sorts earlier in the settings list (within its group).
    order: int = 100
    # The plugin's own Pydantic settings model (co-located in ``plugins/<plugin>/config.py``).
    # The default :meth:`config_schema` derives the generic settings form from it, so a plugin
    # declares its model once instead of re-listing every field. Typed under TYPE_CHECKING to
    # keep this base free of a hard ``pydantic`` import.
    config_model: ClassVar[Optional[type[BaseModel]]] = None

    def on_enable(self, ctx: PluginContext) -> None:
        """Activate the feature. Register seams/hooks here. Called when ticked or at startup."""
        raise NotImplementedError

    def on_disable(self, ctx: PluginContext) -> None:
        """Fully deactivate the feature, removing everything :meth:`on_enable` added."""
        raise NotImplementedError

    def config_schema(self) -> list[ConfigField]:
        """Return the configurable options for the settings GUI.

        Derived from :attr:`config_model` (the plugin's Pydantic settings class) — each scalar
        field becomes a :class:`ConfigField`; complex fields (lists/dicts/nested models) are
        skipped for the bespoke dialogs. Returns ``[]`` when the plugin declares no model.
        """
        if self.config_model is None:
            return []
        # Imported lazily so this base module stays free of a hard config/pydantic dependency
        # (it is referenced only under TYPE_CHECKING above).
        from omnia.core.config.schema import schema_from_model

        return schema_from_model(self.config_model)

    def custom_config_dialog(
        self, repo: ConfigRepository, parent: Any
    ) -> Optional[Any]:
        """Return a bespoke settings ``QDialog`` for this plugin, or None for the generic form.

        Override when the plugin's config can't be expressed as flat :class:`ConfigField`s (e.g.
        a list of rules). The dialog reads/writes via ``repo`` and persists on accept; the
        settings dialog reloads the plugin afterwards. ``parent`` is the Qt parent. Returns
        ``Any``/None so this base stays free of ``aqt`` imports (the override imports Qt lazily).
        """
        return None

    def has_custom_config_dialog(self) -> bool:
        """True if this plugin overrides :meth:`custom_config_dialog` (cheap, no Qt build)."""
        return type(self).custom_config_dialog is not FeaturePlugin.custom_config_dialog
