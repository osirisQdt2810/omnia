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
