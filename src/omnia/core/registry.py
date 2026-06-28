"""Feature-plugin registry.

A feature registers itself with the :func:`register` decorator at import time. The
:class:`~omnia.core.manager.PluginManager` later reads :data:`FEATURE_REGISTRY` to know
which plugins exist. Pure module — no Anki imports.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnia.core.plugin import FeaturePlugin

# Ordered by registration so the settings UI lists plugins deterministically.
FEATURE_REGISTRY: dict[str, type[FeaturePlugin]] = {}


def register(plugin_id: str) -> Callable[[type[FeaturePlugin]], type[FeaturePlugin]]:
    """Register a :class:`FeaturePlugin` subclass under ``plugin_id``.

    Args:
        plugin_id: Unique, stable identifier (snake_case) — also the config namespace key.

    Returns:
        A class decorator that records the class and stamps ``cls.id``.

    Raises:
        ValueError: If ``plugin_id`` is empty or already registered.
    """
    if not plugin_id:
        raise ValueError("plugin_id must be a non-empty string")

    def decorator(cls: type[FeaturePlugin]) -> type[FeaturePlugin]:
        if plugin_id in FEATURE_REGISTRY:
            raise ValueError(f"Duplicate plugin id: {plugin_id!r}")
        cls.id = plugin_id
        FEATURE_REGISTRY[plugin_id] = cls
        return cls

    return decorator


def get_registered() -> dict[str, type[FeaturePlugin]]:
    """Return the registry mapping (a copy, in registration order)."""
    return dict(FEATURE_REGISTRY)
