"""The maintenance-task contract: :class:`NoteView`, :class:`TaskConfigBase`, :class:`MaintenanceTask`.

A maintenance task is a pure function of a note's current text: it reads a :class:`NoteView`
and returns ONLY the fields it wants to change. It never touches a live ``anki.notes.Note``,
never writes, and never reaches for the collection — the runner builds the views, and the
apply step (``apply.py``) is the single place that writes. That split is what lets every task
be unit-tested headless and previewed before anything is persisted.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass(frozen=True)
class NoteView:
    """An immutable snapshot of one note's fields, as tasks see it.

    Frozen so a task cannot smuggle a write through its input: producing a changed note is
    :meth:`with_updates`, which returns a NEW view. ``fields`` keeps the note type's own field
    order (the runner builds it from ``note.items()``).

    Attributes:
        note_id: The note's id.
        note_type: The note type (model) name.
        fields: ``{field name: value}`` — raw stored values, markup included.
    """

    note_id: int
    note_type: str = ""
    fields: Mapping[str, str] = dataclass_field(default_factory=dict)

    def field(self, name: str) -> str:
        """Return field ``name``'s value, or ``""`` when it is absent or empty."""
        return self.fields.get(name, "") or ""

    def with_updates(self, updates: Mapping[str, str]) -> NoteView:
        """Return a copy of this view with ``updates`` applied (the original is untouched)."""
        return NoteView(
            note_id=self.note_id,
            note_type=self.note_type,
            fields={**self.fields, **updates},
        )


class TaskConfigBase(BaseModel):
    """Base settings every maintenance task has: whether it runs, and when.

    Pydantic v1 (see :mod:`omnia.core.config.models` for why v1). ``extra = "forbid"`` so a
    typo in a task's options is reported instead of silently ignored.
    """

    class Config:
        extra = "forbid"

    enable: bool = True
    order: int = 100


class MaintenanceTask(ABC):
    """One deterministic transform over a note's existing text.

    Subclasses declare their options as a :class:`TaskConfigBase` subclass, register with
    ``@register_task("<id>")`` (which stamps :attr:`task_id`), and implement :meth:`process`.
    Instances are cheap and stateless apart from their parsed config, so the runner builds one
    per run.
    """

    # Stamped by @register_task; also the key of the task's config namespace.
    task_id: str = ""
    name: str = ""
    description: str = ""
    config_model: ClassVar[type[TaskConfigBase]] = TaskConfigBase

    def __init__(self, config: TaskConfigBase | None = None) -> None:
        """Initialise the task with its parsed config (defaults when none is given)."""
        self.config = config if config is not None else self.config_model()

    @classmethod
    def from_config(cls, values: Mapping[str, Any]) -> MaintenanceTask:
        """Build the task from its RAW config namespace, validating it against the model.

        Args:
            values: The task's ``[<plugin>.tasks.<task_id>]`` section as a plain dict.

        Returns:
            The configured task.

        Raises:
            pydantic.ValidationError: If ``values`` holds an unknown or invalid option.
        """
        return cls(cls.config_model.parse_obj(dict(values)))

    @property
    def is_enabled(self) -> bool:
        """Whether this task takes part in a run."""
        return bool(self.config.enable)

    @property
    def order(self) -> int:
        """Run position — lower runs first; ties keep registration order."""
        return int(self.config.order)

    @abstractmethod
    def process(self, note: NoteView) -> dict[str, str]:
        """Return ONLY the fields this task wants to change.

        Args:
            note: The note as it stands *after* every earlier task in the run.

        Returns:
            ``{field name: new value}``. An empty dict means "nothing to do" — the runner
            records no change for it. A name the note does not have is dropped by the runner
            (a task reshapes existing text; it cannot add a field to the note type).
        """
