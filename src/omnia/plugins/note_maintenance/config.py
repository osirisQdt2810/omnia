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

    tasks: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        title="Maintenance tasks",
        description=(
            "``{task id: {option: value}}``. Every task takes ``enable`` (whether it runs) and "
            "``order`` (lower runs first) plus its own options. A task with no entry here runs "
            "with its defaults."
        ),
    )
