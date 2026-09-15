"""What the settings page draws for one plugin's options — decided here, in plain Python.

The settings grid is a webview, and until now pressing *Configure…* on a plugin opened a second
window built out of raw Qt widgets: a ``QFormLayout`` of spin boxes and combos, next to a page
made of gradients and animated switches. Two different products, one button apart.

So the form moved into the page. This module is the half of that worth testing: given a
plugin's declared :class:`~omnia.core.plugin.ConfigField` list and its current values, it
produces the JSON the page renders — including **which control** each field gets, which is a
judgement rather than a lookup:

* a ``choice`` of two or three options is a segmented control, not a dropdown. Both registers of
  Phrase Check are visible at once that way, and picking one is a single click instead of a
  click, a scan and a second click;
* a ``choice`` of more is a dropdown, because a segmented control of eight model ids is a wall;
* a number with BOTH bounds is a slider with a live readout — the bounds are the useful part of
  such a setting and a spin box hides them;
* a number with an open end is a plain field, because a slider needs somewhere to end.

No Qt and no ``aqt`` import: the rules above are decided without a QApplication and are checked
without one. The page only draws what it is told.

The Qt renderer (:class:`~omnia.gui.config_form.ConfigFieldEditor`) still exists for the Note
Maintenance per-task panel, which is a bespoke Qt dialog. Both read the same declared
``ConfigField`` list, so the schema remains the one source of truth for what a plugin has —
only the drawing differs, because the hosts do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omnia.core.plugin import ConfigField

#: Up to this many options, a ``choice`` is drawn as a segmented control rather than a dropdown.
#: Three fits one row at the dialog's width; four starts wrapping, and a wrapped segmented
#: control reads as two groups of unrelated buttons.
SEGMENTED_MAX = 3

#: The most steps a slider may span before it stops being a control and becomes a lottery.
#:
#: Bounds alone are not the question — the GRID is. The dialog is 720px wide, so a slider's track
#: is roughly 500; ``overdue_guard.force_again_after_days`` is 0–3650 in steps of one, which is
#: about seven days per pixel. Its default of 7 sits inside the first two pixels with 0 and 13,
#: so nudging it off 7 means never getting back. ``word_lookup.port`` is worse: 64,511 steps.
#:
#: At this ceiling a step is about four pixels, which is a target a hand can actually hit.
#: Anything wider is a number field, where the value is typed and exact.
MAX_SLIDER_STEPS = 120

#: The control names the page knows how to draw. Kept here so a typo is a failing test rather
#: than a field that silently renders as nothing.
CONTROLS = (
    "switch",
    "segmented",
    "dropdown",
    "slider",
    "number",
    "text",
    "secret",
    "color",
)


def control_for(field: ConfigField) -> str:
    """Which control the page should draw for ``field``.

    Args:
        field: The declared option.

    Returns:
        One of :data:`CONTROLS`.
    """
    kind = field.kind
    if kind == "bool":
        return "switch"
    if kind == "choice":
        return "segmented" if len(field.choices) <= SEGMENTED_MAX else "dropdown"
    if kind in ("int", "float"):
        return "slider" if _is_draggable(field) else "number"
    if kind == "color":
        return "color"
    if kind == "secret":
        return "secret"
    return "text"


def _is_draggable(field: ConfigField) -> bool:
    """Whether a number is better dragged than typed.

    Two conditions, and the second is the one that was missing. A slider needs somewhere to stop
    (both bounds), AND few enough stops between them that a particular value can be reached —
    see :data:`MAX_SLIDER_STEPS`.
    """
    if field.minimum is None or field.maximum is None:
        return False
    span = float(field.maximum) - float(field.minimum)
    if span <= 0:
        return False
    return span / _step(field) <= MAX_SLIDER_STEPS


def _step(field: ConfigField) -> float:
    """How far one nudge of a slider moves.

    A float field is a rate or a delay — tenths are what people actually set — and an int field
    steps by one, because half a card is not a thing.
    """
    return 1 if field.kind == "int" else 0.1


def field_payload(field: ConfigField, value: Any) -> dict[str, Any]:
    """One field, as the page needs it.

    ``value`` is normalised through ``getattr(value, "value", value)`` because pydantic v1
    without ``use_enum_values`` hands back an Enum MEMBER rather than its string, and a page
    comparing that against its stringy choices would match nothing and silently show the first
    option as selected.
    """
    control = control_for(field)
    current = getattr(value, "value", value)
    payload: dict[str, Any] = {
        "key": field.key,
        "label": field.label,
        "kind": field.kind,
        "control": control,
        "help": field.help,
        "value": _as_json(current, field),
    }
    if control in ("segmented", "dropdown"):
        payload["choices"] = _choices_with(field, current)
    if control in ("slider", "number"):
        payload["min"] = field.minimum
        payload["max"] = field.maximum
        payload["step"] = _step(field)
    return payload


def _choices_with(field: ConfigField, current: Any) -> list[str]:
    """The declared choices, plus the stored value when it is not among them.

    A value the list does not know about is kept and shown rather than dropped: the alternative
    is a form that silently rewrites a setting to the first option the moment it is opened,
    which is how a hand-edited config or a model id from a newer build gets destroyed by
    somebody merely looking at it.
    """
    choices = [str(choice) for choice in field.choices]
    text = "" if current is None else str(current)
    if text and text not in choices:
        choices.append(text)
    return choices


def _as_json(value: Any, field: ConfigField) -> Any:
    """``value`` in a shape ``json.dumps`` accepts and the page can compare against."""
    if field.kind == "bool":
        return bool(value)
    if field.kind == "int":
        return int(value or 0)
    if field.kind == "float":
        return float(value or 0.0)
    return "" if value is None else str(value)


def panel_payload(
    *,
    plugin_id: str,
    name: str,
    fields: list[ConfigField],
    values: dict[str, Any],
    category: str = "",
    accent: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    """Everything the page needs to draw one plugin's settings.

    Args:
        plugin_id: The plugin's id, echoed back with the save so the page cannot save into the
            wrong namespace.
        name: The heading.
        fields: The plugin's declared options, in order.
        values: Its current settings. A key the settings do not carry falls back to the field's
            own default rather than to an empty control.
        category: The category the plugin belongs to, so Back returns where it came from.
        accent: ``(from, to)`` of the category's gradient, so the panel wears the same colour
            as the tile that opened it.

    Returns:
        A JSON-serializable dict.
    """
    return {
        "id": plugin_id,
        "name": name,
        "category": category,
        "accent": list(accent) if accent else [],
        "fields": [
            field_payload(field, values.get(field.key, field.default))
            for field in fields
        ],
    }
