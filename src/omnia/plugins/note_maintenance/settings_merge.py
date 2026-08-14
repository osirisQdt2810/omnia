"""What a settings save writes back into ``[note_maintenance]``.

Pure module — no ``aqt``/``anki``, no Qt — even though its only caller is the settings dialog:
deciding what SURVIVES a save is config policy, not GUI code, and it is the one thing in this
plugin that can destroy settings the user never sees.

The hazard is ADR-010's, one layer above the models that ADR fixes.
:meth:`~omnia.core.config.repository.ConfigRepository.update_section` merges SHALLOWLY, so
whatever the dialog hands it *is* the stored map afterwards. A dialog that rebuilds that map
out of what THIS build knows — the tasks it registers, the note types this collection has,
the options its form can draw — therefore deletes:

* a ``[note_maintenance.note_types."…"]`` entry for a note type this collection does not have
  (renamed, deleted, or living on another device),
* a ``…tasks.<id>`` section belonging to a task a newer Omnia added and synced down, and
* an option inside a KNOWN task that this version has no renderer for.

:class:`SectionMerge` is the shared answer — start from the RAW stored map and layer edits onto
it — with one subclass per map the dialog rewrites (:class:`NoteTypeSectionMerge`,
:class:`TaskSectionMerge`) and :class:`TaskOptions` keeping the per-option half.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from omnia.core.config.schema import schema_from_model
from omnia.core.plugin import ConfigField
from omnia.plugins.note_maintenance.base import OptionKind, TaskConfigBase


@dataclass(frozen=True)
class OptionRow:
    """One task option the settings form renders.

    Attributes:
        name: The option's config key.
        value: Its current value.
        kind: How to draw it (see :class:`~omnia.plugins.note_maintenance.base.OptionKind`).
        field: The scalar descriptor to render with, or None for a
            :attr:`~omnia.plugins.note_maintenance.base.OptionKind.FIELD_MAP` row (which needs
            a bespoke editor).
    """

    name: str
    value: Any
    kind: OptionKind
    field: Optional[ConfigField] = None


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
        rows: The renderable options as :class:`OptionRow`s, in model order.
        passthrough: The options with no renderer, kept verbatim for the save.
    """

    def __init__(self, config: TaskConfigBase) -> None:
        """Classify ``config``'s options.

        Args:
            config: The task's parsed settings (an instance, so it carries current values).
        """
        self.model: type[TaskConfigBase] = type(config)
        scalars = {field.key: field for field in schema_from_model(self.model)}
        declared = self.model.__fields__
        self.rows: list[OptionRow] = []
        self.passthrough: dict[str, Any] = {}
        for name, value in config.dict().items():
            if name == "enable":
                continue
            kind = self.model.option_kind(name)
            if name in scalars:
                self.rows.append(
                    OptionRow(
                        name=name,
                        value=value,
                        # Only a NOTE_FIELD scalar is special; anything else the scalar deriver
                        # understood is drawn by the shared editor.
                        kind=(
                            OptionKind.NOTE_FIELD
                            if kind is OptionKind.NOTE_FIELD
                            else OptionKind.SCALAR
                        ),
                        field=scalars[name],
                    )
                )
            elif (
                kind is OptionKind.FIELD_MAP
                and name in declared
                and isinstance(value, dict)
            ):
                # A DECLARED field map — the one complex shape the scalar deriver skips and the
                # panel renders with its bespoke editor. An UNDECLARED key that happens to hold
                # a table is not that: the model kept it verbatim (ADR-010) and no renderer
                # knows what it means, so it goes through untouched.
                self.rows.append(OptionRow(name=name, value=value, kind=kind))
            else:
                self.passthrough[name] = value


class SectionMerge:
    """A stored map of entries, updated entry by entry — never rebuilt from this build's world.

    Built on the RAW stored map because that map is replaced wholesale by the save (see the
    module docstring). An entry this build does not touch is handed back exactly as it came in,
    whatever shape it has: a key this version does not know, and even a value that is not a
    table at all, survive the round trip.
    """

    def __init__(self, stored: Mapping[str, Any]) -> None:
        """Start from ``stored``.

        Args:
            stored: The map as it is stored, ``{key: values}``. Copied, so applying an edit
                never mutates the caller's map.
        """
        self._entries: dict[str, Any] = {
            key: dict(values) if isinstance(values, dict) else values
            for key, values in stored.items()
        }

    def result(self) -> dict[str, Any]:
        """Return the merged map to persist (a copy — the merge keeps its own)."""
        return dict(self._entries)

    def _merge(self, key: str, values: Mapping[str, Any]) -> None:
        """Layer ``values`` over what was stored under ``key``.

        A stored entry that is not a table cannot be merged onto; the edited values simply
        replace it, which is what the user just asked for by editing that entry.
        """
        base = self._entries.get(key)
        self._entries[key] = {
            **(base if isinstance(base, dict) else {}),
            **values,
        }


class TaskSectionMerge(SectionMerge):
    """The ``tasks`` map of ONE note type: what was stored, updated with what was edited."""

    def apply(self, task_id: str, *, enable: bool, options: Mapping[str, Any]) -> None:
        """Layer one task's edited switch and options over what was stored for it.

        Args:
            task_id: The task whose section this is.
            enable: Whether the task takes part in a run (the task list's tick owns it, so it
                is written here rather than coming through ``options``).
            options: What the task's form reports — its rendered rows plus whatever it could
                not draw, unchanged.
        """
        self._merge(task_id, {"enable": bool(enable), **options})


class NoteTypeSectionMerge(SectionMerge):
    """The whole ``note_types`` map: what was stored, updated with what was edited."""

    def apply(
        self,
        note_type: str,
        *,
        enable: bool,
        tasks: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Layer one note type's edited tick (and task map) over what was stored for it.

        Args:
            note_type: The note type whose entry this is, in its STORED spelling where it has
                one — so ticking a note type cannot fork its settings into a second entry that
                differs only in case.
            enable: Whether the note type takes part in a run.
            tasks: Its edited task map, or None when the user never opened it — the stored task
                map is then left exactly as it is, which is the difference between "ticked a
                note type" and "reconfigured it".
        """
        values: dict[str, Any] = {"enable": bool(enable)}
        if tasks is not None:
            values["tasks"] = dict(tasks)
        self._merge(note_type, values)
