"""Note-maintenance settings (the plugin's own Pydantic v1 config).

Co-located with the plugin, like every other feature. The per-task options are deliberately
kept as a RAW ``{task_id: {option: value}}`` map here: each task owns its own
:class:`~omnia.plugins.note_maintenance.base.TaskConfigBase` subclass and validates its own
section (see :func:`~omnia.plugins.note_maintenance.registry.build_tasks`), so this model does
not have to know — or be edited for — every task that exists.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from omnia.core.config.base import PersistedModel


class NoteMaintenanceSettings(PersistedModel):
    """Settings for the note-maintenance feature."""

    # The VALUE is deliberately ``Any``, not ``dict[str, Any]``: a stricter annotation is the one
    # layer ADR-010's tolerance could not reach. A task entry that is not a table (a newer Omnia
    # reshaping it, or a hand-edited features.toml) would fail validation here, and
    # ``PluginManager._activate`` swallows that into "the feature silently never enables" —
    # losing the Browser entry with only a log line. Readers already treat a non-table entry as
    # "no stored options" (``build_tasks``, ``TaskSectionMerge``), so tolerating it costs nothing.
    tasks: dict[str, Any] = Field(
        default_factory=dict,
        title="Maintenance tasks",
        description=(
            "``{task id: {option: value}}``. Every task takes ``enable`` (whether it runs) and "
            "``order`` (lower runs first) plus its own options. A task with no entry here runs "
            "with its defaults. An entry that is not a table is kept verbatim and read as "
            "'no stored options'."
        ),
    )
