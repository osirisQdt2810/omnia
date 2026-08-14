"""Note-maintenance settings (the plugin's own Pydantic v1 config).

Co-located with the plugin, like every other feature. Task options are stored PER NOTE TYPE
(``note_types``) because a task option that names a field can only mean something inside one
note type — see :mod:`~omnia.plugins.note_maintenance.note_types` for the shape and for what
an OLDER Omnia does with it.

Both maps are deliberately kept RAW (``dict[str, Any]``): each task owns its own
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

    # The VALUE of both maps is deliberately ``Any``, not ``dict[str, Any]``: a stricter
    # annotation is the one layer ADR-010's tolerance could not reach. An entry that is not a
    # table (a newer Omnia reshaping it, or a hand-edited features.toml) would fail validation
    # here, and ``PluginManager._activate`` swallows that into "the feature silently never
    # enables" — losing the Browser entry with only a log line. Readers already treat a
    # non-table entry as "no stored settings", so tolerating it costs nothing.
    note_types: dict[str, Any] = Field(
        default_factory=dict,
        title="Note types to maintain",
        description=(
            "``{note type name: {enable, tasks}}``. A run applies each note's OWN note type's "
            "settings, so several note types can be configured and maintained in one pass. A "
            "note whose type has no entry here is skipped and reported, never guessed at."
        ),
    )
    tasks: dict[str, Any] = Field(
        default_factory=dict,
        title="Maintenance tasks (pre-note-type)",
        description=(
            "The global task map Omnia used before task settings became per-note-type. It is "
            "no longer run: field names differ per note type, so one map could not fit a "
            "collection. It is kept verbatim — an OLDER Omnia still runs it, and the settings "
            "panel offers it as the starting point for a note type that has none yet."
        ),
    )
