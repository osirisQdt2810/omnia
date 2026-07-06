"""Typing-accuracy settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin; the generic settings form is derived from it via
:func:`omnia.core.config.schema.schema_from_model`. Field descriptions become GUI tooltips
and ``ge``/``le`` bounds drive the numeric widgets.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class TypedAccuracySettings(_Strict):
    """Settings for the typing-accuracy grader."""

    threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the typed answer that must be correct to count as a pass.\n"
            "• 0.7 = 70% of characters right.\n"
            "• At or above this → the pass ease below is staged.\n"
            "• Below this → the card is forced to Hard."
        ),
    )
    # Auto-answer on a pass: "good"/"easy" stage that ease; "no" stages nothing (the user's
    # own press stands). A fail always forces Hard regardless of this setting. ``Literal``
    # both validates the value and drives the generic form's choice widget.
    pass_ease: Literal["good", "easy", "no"] = Field(
        "good",
        description=(
            "Which ease to auto-stage when the typed answer passes.\n"
            "• good / easy: stage that grade for you on a pass.\n"
            "• no: stage nothing — your own key press stands.\n"
            "• A fail always forces Hard, regardless of this setting."
        ),
    )
    show_stats: bool = Field(
        True,
        description=(
            "Add a typed-accuracy panel to Anki's Statistics screen.\n"
            "• An interactive donut plus a Good/Bad/Miss/Empty breakdown.\n"
            "• Off: the grader still runs; only the stats panel is hidden."
        ),
    )
