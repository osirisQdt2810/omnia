"""What a settings save writes back into ``[note_maintenance.tasks]``.

Pure module — no ``aqt``/``anki``, no Qt — even though its only caller is the settings dialog:
deciding what SURVIVES a save is config policy, not GUI code, and it is the one thing in this
plugin that can destroy settings the user never sees.

The hazard is ADR-010's, one layer above the models that ADR fixes.
:meth:`~omnia.core.config.repository.ConfigRepository.update_section` merges SHALLOWLY, so
whatever the dialog hands it *is* the stored ``tasks`` map afterwards. A dialog that rebuilds
that map out of the tasks THIS build registers, carrying only the options its form could draw,
therefore deletes:

* a ``[note_maintenance.tasks.<id>]`` section belonging to a task a newer Omnia added and
  synced down, and
* an option inside a KNOWN task that this version has no renderer for.

:class:`TaskOptions` keeps the second kind (per task) and :class:`TaskSectionMerge` the first
(per map), both starting from the RAW stored section so nothing they keep depends on that
section parsing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from pydantic import BaseModel

from omnia.core.config.schema import schema_from_model
from omnia.core.plugin import ConfigField


class TaskOptions:
    """Splits ONE task's saved options into the rows a form renders and the rest.

    The interesting half is what a form CANNOT render: a list-shaped option, or a key a newer
    Omnia wrote that this version has never heard of. Those are carried through the save
    untouched — a settings dialog that writes back only what it could draw deletes the settings
    it did not understand.

    ``enable`` is dropped from both halves: the task list's tick owns it and the dialog writes
    it back itself (see :meth:`TaskSectionMerge.apply`).

    Attributes:
        model: The task's settings model (for a complex row's title/help).
        rows: ``(name, value, field)`` in model order — ``field`` is the scalar descriptor to
            render with, or None for a field mapping (which needs a bespoke editor).
        passthrough: The options with no renderer, kept verbatim for the save.
    """

    def __init__(self, config: BaseModel) -> None:
        """Classify ``config``'s options.

        Args:
            config: The task's parsed settings (an instance, so it carries current values).
        """
        self.model = type(config)
        scalars = {field.key: field for field in schema_from_model(self.model)}
        self.rows: list[tuple[str, Any, Optional[ConfigField]]] = []
        self.passthrough: dict[str, Any] = {}
        for name, value in config.dict().items():
            if name == "enable":
                continue
            if isinstance(value, dict):
                self.rows.append((name, value, None))
            elif name in scalars:
                self.rows.append((name, value, scalars[name]))
            else:
                self.passthrough[name] = value


class TaskSectionMerge:
    """The whole ``tasks`` map to persist: what was stored, updated with what was edited.

    Built on the RAW stored map (never on the registry alone) because that map is replaced
    wholesale by the save — see the module docstring. An entry this build does not touch is
    handed back exactly as it came in, whatever shape it has: a task id this version does not
    register, and even a value that is not a table at all, survive the round trip.
    """

    def __init__(self, stored: Mapping[str, Any]) -> None:
        """Start from ``stored``.

        Args:
            stored: The ``[note_maintenance.tasks]`` map as it is stored, ``{task id: values}``.
                Copied, so applying an edit never mutates the caller's map.
        """
        self._tasks: dict[str, Any] = {
            task_id: dict(values) if isinstance(values, dict) else values
            for task_id, values in stored.items()
        }

    def apply(self, task_id: str, *, enable: bool, options: Mapping[str, Any]) -> None:
        """Layer one task's edited switch and options over what was stored for it.

        Args:
            task_id: The task whose section this is.
            enable: Whether the task takes part in a run (the task list's tick owns it, so it
                is written here rather than coming through ``options``).
            options: What the task's form reports — its rendered rows plus whatever it could
                not draw, unchanged.
        """
        base = self._tasks.get(task_id)
        # A stored entry that is not a table cannot be merged onto; the edited values simply
        # replace it, which is what the user just asked for by editing that task.
        self._tasks[task_id] = {
            **(base if isinstance(base, dict) else {}),
            "enable": bool(enable),
            **options,
        }

    def result(self) -> dict[str, Any]:
        """Return the merged map to persist (a copy — the merge keeps its own)."""
        return dict(self._tasks)
