"""Derive a settings GUI schema from a Pydantic v1 model.

Each plugin owns a Pydantic settings model (in ``plugins/<plugin>/config.py``); the generic
settings form should not duplicate that as a hand-written :class:`ConfigField` list. This
module introspects a model's ``__fields__`` and emits the equivalent :class:`ConfigField`s:

* field type -> kind (``bool``/``int``/``float``/``text``; ``str`` named like a credential ->
  ``secret``; ``Literal[...]`` / ``Enum`` -> ``choice`` with its values),
* ``Field(..., description=)`` -> the field's ``help`` (the GUI tooltip),
* ``ge``/``le`` -> ``minimum``/``maximum``,
* the field's default -> ``default``.

Complex fields (typed lists/dicts and nested models, e.g. ``per_deck`` / ``note_types`` /
``fields``) are SKIPPED — they are edited by bespoke dialogs, not the generic form. Pure
module: no ``aqt``/``anki`` imports, so it unit-tests headless.
"""

from __future__ import annotations

import enum
import typing
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic.fields import SHAPE_SINGLETON

from omnia.core.plugin import ConfigField

if TYPE_CHECKING:
    from pydantic.fields import ModelField

# A ``str`` field whose name contains one of these substrings is rendered as a masked
# ``secret`` input rather than a plain text box.
_SECRET_HINTS = ("api_key", "access_token", "secret", "password", "token")


def schema_from_model(model_cls: type[BaseModel]) -> list[ConfigField]:
    """Return the generic-form :class:`ConfigField` list for a Pydantic v1 settings model.

    Args:
        model_cls: A Pydantic v1 ``BaseModel`` subclass (a plugin's settings model).

    Returns:
        One :class:`ConfigField` per scalar field, in declaration order. Complex fields
        (typed lists/dicts, nested models) are omitted — they need a bespoke dialog.
    """
    fields: list[ConfigField] = []
    for name, model_field in model_cls.__fields__.items():
        config_field = _field_to_config(name, model_field)
        if config_field is not None:
            fields.append(config_field)
    return fields


def _field_to_config(name: str, model_field: ModelField) -> ConfigField | None:
    """Map one Pydantic field to a :class:`ConfigField`, or None to skip it."""
    if _is_complex(model_field):
        return None

    field_info = model_field.field_info
    kind, choices = _kind_and_choices(name, model_field)
    return ConfigField(
        key=name,
        label=_label(name),
        kind=kind,
        default=model_field.default,
        help=field_info.description or "",
        choices=choices,
        minimum=field_info.ge,
        maximum=field_info.le,
    )


def _is_complex(model_field: ModelField) -> bool:
    """True for fields the generic form can't render (lists, dicts, nested models)."""
    if model_field.shape != SHAPE_SINGLETON:  # typed list/dict (e.g. dict[str, X])
        return True
    field_type = model_field.type_
    if isinstance(field_type, type) and issubclass(field_type, BaseModel):
        return True
    # Bare ``list`` / ``dict`` annotations keep SHAPE_SINGLETON but aren't scalars.
    return field_type in (list, dict)


def _kind_and_choices(
    name: str, model_field: ModelField
) -> tuple[str, tuple[str, ...]]:
    """Return the GUI ``kind`` and choice tuple for a scalar field."""
    outer = model_field.outer_type_
    if typing.get_origin(outer) is typing.Literal:
        return "choice", tuple(str(value) for value in typing.get_args(outer))

    field_type = model_field.type_
    if isinstance(field_type, type) and issubclass(field_type, enum.Enum):
        return "choice", tuple(str(member.value) for member in field_type)
    # bool must be checked before int: ``bool`` is a subclass of ``int``.
    if isinstance(field_type, type) and issubclass(field_type, bool):
        return "bool", ()
    if isinstance(field_type, type) and issubclass(field_type, int):
        return "int", ()
    if isinstance(field_type, type) and issubclass(field_type, float):
        return "float", ()
    if _looks_secret(name):
        return "secret", ()
    return "text", ()


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _label(name: str) -> str:
    """Humanise a snake_case field name into a Title Case label."""
    return name.replace("_", " ").strip().capitalize()
