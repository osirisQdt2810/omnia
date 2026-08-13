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


def build_tasks(configs: Mapping[str, Any]) -> list[MaintenanceTask]:
    """Instantiate every registered task, configured from ``configs``.

    NEVER raises: this is the one gate between stored config and a task, and both callers (the
    Browser run and the settings panel) are Qt slots, where an exception is an Anki traceback
    dialog. A section this version cannot parse — a hand-edited ``features.toml``, or config
    written by a NEWER Omnia and synced down — degrades to that ONE task's shipped defaults
    for the values it cannot read (see :func:`_build_task`) and is logged; the other tasks keep
    the user's settings.

    A registered task with no entry in ``configs`` is built from its model defaults, so a
    fresh install still has working tasks. An entry naming an UNKNOWN task is ignored — a task
    may have been removed while the user's config still mentions it, and that must not brick
    the whole plugin.

    Args:
        configs: The plugin's ``tasks`` namespace, ``{task_id: {option: value}}``, RAW: the
            settings panel hands it over unvalidated (that is what keeps a section this build
            cannot read alive through a save), so an entry may be any shape at all.

    Returns:
        One task instance per registered task, in registration order. Enabling/ordering is the
        runner's business (see :class:`~omnia.plugins.note_maintenance.runner.MaintenanceRunner`).
    """
    return [
        _build_task(task_id, task_cls, configs.get(task_id, {}))
        for task_id, task_cls in TASK_REGISTRY.items()
    ]


def _build_task(
    task_id: str, task_cls: type[MaintenanceTask], values: Any
) -> MaintenanceTask:
    """Build one task from ``values``, keeping every value it can read.

    ONE unreadable value must cost the user only itself. Reverting the whole section instead
    would revert ``enable``/``order`` — the task's SWITCHES — turning a task the user switched
    OFF back on, and would revert options like a find/replace pair that decide what a run does
    to their notes. So the fallback re-parses the section without the values this version
    cannot read (:func:`_readable_options`), and only those revert to the task's defaults.
    """
    if not isinstance(values, Mapping):
        # Not a table at all (hand-edited config, or a shape a newer Omnia introduced). There
        # is nothing to read a value out of, so the whole section reverts.
        logger.error(
            "note_maintenance: task %r settings are not a table; using its defaults",
            task_id,
        )
        return task_cls()
    try:
        return task_cls.from_config(values)
    except ValidationError:
        logger.exception(
            "note_maintenance: task %r has invalid settings; using its defaults",
            task_id,
        )
    try:
        return task_cls.from_config(_readable_options(task_cls, values))
    except ValidationError:
        # Field-by-field readable does not mean parseable TOGETHER: a cross-field validator can
        # still reject the salvaged subset, and this is a Qt slot. Everything reverts, but the
        # run survives. (A task whose OWN defaults do not validate is a bug in the task, not
        # stored data, and is caught by the bundled-defaults test.)
        logger.exception(
            "note_maintenance: task %r rejects its readable settings; using its defaults",
            task_id,
        )
        return task_cls()


def _readable_options(
    task_cls: type[MaintenanceTask], values: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the entries of ``values`` that validate on their OWN, in stored order.

    Field-by-field (pydantic v1 ``ModelField.validate``) rather than by re-parsing the model:
    the section is already known not to parse as a whole, and a garbage ``order`` must not take
    a perfectly good ``enable`` — or the user's find/replace text — down with it.

    A key the model does not declare is left out: it is the one thing this version genuinely
    cannot read, and it is kept where it matters — the settings panel merges onto the RAW
    stored section (see :mod:`~omnia.plugins.note_maintenance.settings_merge`), so a save never
    drops it.
    """
    kept: dict[str, Any] = {}
    for key, value in values.items():
        field = task_cls.config_model.__fields__.get(key)
        if field is None:
            continue
        _parsed, error = field.validate(value, {}, loc=key)
        if error is None:
            kept[key] = value
    return kept
