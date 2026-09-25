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

import dataclasses
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
    "range",
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
        if not _is_draggable(field):
            return "number"
        # Two marks on one axis are one decision. Drawn as one track with two handles, the
        # relationship between them — which is the whole content of the setting — is visible
        # without reading either number.
        return "range" if field.upper_key else "slider"
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


#: A float bounded to 0..1 is a FRACTION — an accuracy ratio, a share of a card — and reads as
#: a percentage. Tenths are too coarse for one: "95% correct" is a setting people actually want
#: and 0.1 cannot express it. Twenty steps is still a comfortable drag.
_FRACTION_STEP = 0.05


def _step(field: ConfigField) -> float:
    """How far one nudge of a slider moves.

    An int steps by one, because half a card is not a thing. A float is a rate or a delay, where
    tenths are what people set — unless it is bounded to 0..1, which makes it a fraction and
    tenths too blunt to place a threshold with.
    """
    if field.kind == "int":
        return 1
    if field.minimum == 0.0 and field.maximum == 1.0:
        return _FRACTION_STEP
    return 0.1


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
    if control in ("slider", "number", "range"):
        payload["min"] = field.minimum
        payload["max"] = field.maximum
        payload["step"] = _step(field)
    if control == "range":
        payload["upperKey"] = field.upper_key
    return payload


def pair_payload(
    lower: ConfigField, upper: ConfigField, lower_value: Any, upper_value: Any
) -> dict[str, Any]:
    """The two-handled track for ``lower`` and its declared ``upper`` partner.

    Built from BOTH descriptors rather than from the lower one alone, because the page has to
    label each handle and write back two keys. The bounds come from the lower field: one track
    can only have one scale, and two fields cutting the same axis that disagreed about its ends
    would be a bug in the settings model rather than something to render.

    Args:
        lower: The field declaring ``upper_key``.
        upper: The field it names.
        lower_value: Current value of the lower mark.
        upper_value: Current value of the upper mark.

    Returns:
        The lower field's payload, plus an ``upper`` block for the second handle.
    """
    payload = field_payload(lower, lower_value)
    payload["upper"] = {
        "key": upper.key,
        "label": upper.label,
        "help": upper.help,
        "value": _as_json(getattr(upper_value, "value", upper_value), upper),
    }
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
    """``value`` in a shape ``json.dumps`` accepts and the page can compare against.

    A stored value that will not coerce falls back to the field's DEFAULT rather than raising.
    It is reached with a raw, unvalidated section — that is the point of the caller's fallback,
    which exists so a section the settings model refuses can still be opened and corrected —
    and a number box has no way to hold ``"soon"``. Raising here would put the panel back
    exactly where it could not be opened, one layer down.

    ``_choices_with`` keeps an unrecognised value instead, and the difference is what the
    control can represent: a dropdown can carry one more option, a number input cannot carry a
    word. Neither DESTROYS anything on its own — nothing is written until the reader saves.
    """
    try:
        if field.kind == "bool":
            return bool(value)
        if field.kind == "int":
            return int(value or 0)
        if field.kind == "float":
            return float(value or 0.0)
    except (TypeError, ValueError):
        return _fallback(field)
    return "" if value is None else str(value)


def _fallback(field: ConfigField) -> Any:
    """Something of the field's own kind, for when the stored value is not.

    The declared default first, since that is what the plugin considers reasonable; a blank of
    the right type if even that will not coerce (a hand-written ``ConfigField``, not a shape
    this add-on ships). Deliberately not recursive into :func:`_as_json` — a bad default would
    then bounce between the two.
    """
    try:
        if field.kind == "bool":
            return bool(field.default)
        if field.kind == "int":
            return int(field.default or 0)
        if field.kind == "float":
            return float(field.default or 0.0)
    except (TypeError, ValueError):
        pass
    return {"bool": False, "int": 0, "float": 0.0}.get(field.kind, "")


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
        "fields": _field_payloads(fields, values),
    }


def _pair_partner(
    field: ConfigField, by_key: dict[str, ConfigField]
) -> Optional[ConfigField]:
    """The second handle for ``field``, or ``None`` when the two cannot share one track.

    Two reasons a declared pairing does not resolve, and both end the same way — each setting
    keeps its own row:

    * the named field is not there (renamed, removed, or spelt wrong);
    * the two disagree about the scale. One track has one set of ends, and the payload carries
      the LOWER field's, so an upper mark declaring a wider range would be silently clamped on
      save — a setting rewritten to a value its own declaration allows, by a control that never
      offered the rest of it.

    Unpairing rather than raising keeps the failure in the settings model from reaching the
    user as a dialog that will not open: every option is still on screen and still settable.
    """
    if not field.upper_key:
        return None
    upper = by_key.get(field.upper_key)
    if upper is None:
        return None
    if (upper.minimum, upper.maximum) != (field.minimum, field.maximum):
        return None
    return upper


def _field_payloads(
    fields: list[ConfigField], values: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every field as the page needs it, with declared pairs folded into one control.

    A field named by another's ``upper_key`` is NOT emitted on its own: it is the second handle
    of that track, and drawing it twice would give the user two ways to set one number that
    could disagree on screen.

    An ``upper_key`` naming a field that is not there — renamed, removed, or spelt wrong — falls
    back to two ordinary sliders rather than dropping a setting. A settings page that silently
    stops showing an option is a worse failure than one that shows it plainly.
    """
    by_key = {field.key: field for field in fields}
    # Resolved ONCE and reused, so "this field is the second handle of another" and "this field
    # has a second handle" can never disagree. Asking the question twice is how a rejected
    # partner gets skipped as paired-away and then never drawn at all.
    partners = {field.key: _pair_partner(field, by_key) for field in fields}
    paired_away = {upper.key for upper in partners.values() if upper is not None}
    payloads: list[dict[str, Any]] = []
    for field in fields:
        if field.key in paired_away:
            continue
        upper = partners[field.key]
        if upper is None:
            # Strip the dangling pairing before building: `control_for` reads `upper_key` and
            # would say "range" for a payload with no second handle in it, which the page
            # cannot draw. A descriptor whose partner does not resolve is simply not paired.
            solo = (
                dataclasses.replace(field, upper_key="") if field.upper_key else field
            )
            payloads.append(field_payload(solo, values.get(solo.key, solo.default)))
            continue
        payloads.append(
            pair_payload(
                field,
                upper,
                values.get(field.key, field.default),
                values.get(upper.key, upper.default),
            )
        )
    return payloads
