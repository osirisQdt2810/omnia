"""Display-interval settings model (the plugin's own Pydantic v1 config).

The generic settings form is derived from this model via
:func:`omnia.core.config.schema.schema_from_model`; the ``text_color`` field is named like a
colour so the deriver renders it with a colour picker.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class DisplayIntervalSettings(_Strict):
    """Settings for the next-interval grading-bar label."""

    text_color: str = Field(
        "#c62828",
        title="Text color",
        description=(
            'Colour of the "interval: X" label shown in the grading bar.\n'
            "• Appears on the answer side, at the bottom-right of the "
            "Again/Hard/Good/Easy bar.\n"
            "• A subtle shadow keeps it legible on both light and dark themes.\n"
            "• Default: #c62828 (a muted red)."
        ),
    )
    expose_to_templates: bool = Field(
        True,
        title="Expose to card templates",
        description=(
            "Give card templates the predicted next interval as `window.omniaIntervals`.\n"
            "• On: the answer side gets {next_seconds, next_days, next_label, current_days}\n"
            "  BEFORE the template's own scripts run, so template JS can branch on it\n"
            "  (e.g. play the definition while an answer is still shaky, the example once\n"
            "  it is well learned).\n"
            "• The value is the same pipeline preview as the grading-bar label: a Good press\n"
            "  folded through omnia's ease transformers (overdue_guard reflected;\n"
            "  typed_accuracy is not — its ease arrives async).\n"
            "• Only injected while at least one ease transformer is active (overdue_guard /\n"
            "  typed_accuracy on) — otherwise, and when this is Off, templates see no omnia\n"
            "  variables and their own fallback (the current interval) applies."
        ),
    )
