"""Maintenance-task self-registration registry (plugin-local).

Mirrors the add-on's other registries (:mod:`omnia.core.registry`,
:mod:`omnia.core.providers.tts.registry`): a task registers itself with the
:func:`register_task` decorator at import time, and :func:`build_tasks` turns the plugin's raw
config namespace into configured task instances.

Tasks are deliberately NOT feature plugins: they are numerous, homogeneous and share one
runner, one preview and one apply path, so they live in this plugin-local registry instead of
flooding the settings list. Pure module — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from omnia.core.logging import get_logger
from omnia.plugins.note_maintenance.base import MaintenanceTask

logger = get_logger("note_maintenance")

# task id -> task class, in registration order (the settings UI lists them this way).
TASK_REGISTRY: dict[str, type[MaintenanceTask]] = {}


def register_task(
    task_id: str,
) -> Callable[[type[MaintenanceTask]], type[MaintenanceTask]]:
    """Register a :class:`MaintenanceTask` subclass under ``task_id``.

    Args:
        task_id: Unique, stable identifier (snake_case) — also the key of the task's config
            namespace under ``[note_maintenance.tasks]``.

    Returns:
        A class decorator that records the class and stamps ``cls.task_id``.

    Raises:
        ValueError: If ``task_id`` is empty or already bound to a different class.
    """
    if not task_id:
        raise ValueError("task_id must be a non-empty string")

    def decorator(cls: type[MaintenanceTask]) -> type[MaintenanceTask]:
        existing = TASK_REGISTRY.get(task_id)
        if existing is not None and existing is not cls:
            raise ValueError(f"Duplicate maintenance task id: {task_id!r}")
        cls.task_id = task_id
        TASK_REGISTRY[task_id] = cls
        return cls

    return decorator


def registered_tasks() -> dict[str, type[MaintenanceTask]]:
    """Return the registry mapping (a copy, in registration order)."""
    return dict(TASK_REGISTRY)


def build_tasks(configs: Mapping[str, Mapping[str, Any]]) -> list[MaintenanceTask]:
    """Instantiate every registered task, configured from ``configs``.

    NEVER raises: this is the one gate between stored config and a task, and both callers (the
    Browser run and the settings panel) are Qt slots, where an exception is an Anki traceback
    dialog. A section this version cannot parse — a hand-edited ``features.toml``, or config
    written by a NEWER Omnia and synced down — degrades to that ONE task's shipped defaults and
    is logged; the other tasks keep the user's settings.

    A registered task with no entry in ``configs`` is built from its model defaults, so a
    fresh install still has working tasks. An entry naming an UNKNOWN task is ignored — a task
    may have been removed while the user's config still mentions it, and that must not brick
    the whole plugin.

    Args:
        configs: The plugin's ``tasks`` namespace, ``{task_id: {option: value}}``.

    Returns:
        One task instance per registered task, in registration order. Enabling/ordering is the
        runner's business (see :class:`~omnia.plugins.note_maintenance.runner.MaintenanceRunner`).
    """
    return [
        _build_task(task_id, task_cls, configs.get(task_id, {}))
        for task_id, task_cls in TASK_REGISTRY.items()
    ]


def _build_task(
    task_id: str, task_cls: type[MaintenanceTask], values: Mapping[str, Any]
) -> MaintenanceTask:
    """Build one task from ``values``, falling back to its defaults if they don't parse."""
    try:
        return task_cls.from_config(values)
    except ValidationError:
        logger.exception(
            "note_maintenance: task %r has invalid settings; using its defaults",
            task_id,
        )
        return task_cls()
