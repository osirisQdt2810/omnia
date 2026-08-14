"""The maintenance-task contract: :class:`NoteView`, :class:`TaskConfigBase`, :class:`MaintenanceTask`.

A maintenance task is a pure function of a note's current text: it reads a :class:`NoteView`
and returns ONLY the fields it wants to change. It never touches a live ``anki.notes.Note``,
never writes, and never reaches for the collection — the runner builds the views, and the
apply step (``apply.py``) is the single place that writes. That split is what lets every task
be unit-tested headless and previewed before anything is persisted.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, ClassVar

from pydantic import Field

from omnia.core.config.base import PersistedModel

# The ``Field(..., renders_as=…)`` keyword a task uses to say what an option HOLDS. Pydantic v1
# parks an unknown Field keyword in ``field_info.extra``, so the statement lives on the option
# itself instead of in a table the panel would have to be edited to keep in step.
_RENDERS_AS = "renders_as"


class OptionKind(enum.Enum):
    """What a task option holds, and therefore how the settings panel must render it.

    A field name typed by hand fails SILENTLY at run time — the task simply finds nothing —
    and field names differ per note type, so the panel offers the selected note type's real
    fields instead. Only the task knows which of its options are field names, hence the
    declaration at the option:

    ``field: str = Field("Synonyms", renders_as=OptionKind.NOTE_FIELD)``
    """

    #: A plain value (number, text, switch) — rendered by the shared ``ConfigFieldEditor``.
    SCALAR = "scalar"
    #: ONE field name of the note type being configured — rendered as a dropdown.
    NOTE_FIELD = "note_field"
    #: A ``{source field: target field}`` map — rendered as two columns of dropdowns.
    FIELD_MAP = "field_map"


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


class TaskConfigBase(PersistedModel):
    """Base settings every maintenance task has: whether it runs, and when.

    A task's options are persisted config (``[note_maintenance.tasks.<id>]`` in the synced
    collection config), so this extends the shared
    :class:`~omnia.core.config.base.PersistedModel`: an option key a NEWER Omnia added rides
    through a load and back into storage untouched, instead of costing the older device the
    whole section (ADR-010).

    The values here are the CONTRACT's fallback, not the shipped defaults: a task that ships
    off, or in a particular position, re-declares ``enable``/``order`` on its own config model.
    Defaults have to live in the model because that is the only layer every storage backend
    reads — with the collection backend (ADR-006) the bundled ``features.example.toml`` is
    never loaded, and the file it mirrors is created fresh from these models.
    """

    enable: bool = Field(
        True,
        title="Run this task",
        description="Whether this task takes part in a maintenance run.",
    )
    order: int = Field(
        100,
        title="Run order",
        description=(
            "Run position — lower runs first; ties keep registration order. A task that "
            "declares no order of its own runs last. This is the DEFAULT position, not a "
            "box anyone fills in: the settings panel owns the order through its ▲/▼ "
            "buttons and stamps this from the list. It stays persisted because the runner "
            "sorts on it and an older Omnia reads it."
        ),
    )

    @classmethod
    def option_kind(cls, name: str) -> OptionKind:
        """Return what option ``name`` holds — see :class:`OptionKind`.

        Args:
            name: An option of this model. A name the model does not declare (a key a NEWER
                Omnia wrote, kept by ADR-010) is :attr:`OptionKind.SCALAR`: nothing here knows
                what it means, and the panel carries it through the save untouched.

        Returns:
            The declared kind, or :attr:`OptionKind.SCALAR` when the option declares none.
        """
        model_field = cls.__fields__.get(name)
        if model_field is None:
            return OptionKind.SCALAR
        declared = model_field.field_info.extra.get(_RENDERS_AS)
        # A kind that is not an OptionKind raises: that is OUR source being wrong, not stored
        # data, so it must fail loudly rather than degrade to a text box.
        return OptionKind(declared) if declared is not None else OptionKind.SCALAR


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
            pydantic.ValidationError: If ``values`` holds an invalid value for a declared
                option. An option this version does not declare is KEPT, not rejected — the
                section is persisted config (see :class:`TaskConfigBase`).
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
