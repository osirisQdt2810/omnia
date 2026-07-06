"""Overdue-guard settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin; the generic settings form is derived from it via
:func:`omnia.core.config.schema.schema_from_model`. Field descriptions become GUI tooltips
and ``ge``/``le`` bounds drive the numeric widgets.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class OverdueGuardSettings(_Strict):
    """Settings for the overdue guard."""

    ratio: float = Field(
        0.8,
        ge=0.0,
        le=10.0,
        title="Overdue Ratio",
        description=(
            "When you answer an OVERDUE card, Overdue Guard caps the grade down to Hard "
            "(or Again) — so a card you forgot for a long time can't jump to a big interval "
            "just because you pressed Good/Easy. It only ever lowers a grade.\n\n"
            "This value sets how late counts as 'overdue': overdue when "
            "days-late ÷ the card's current interval ≥ this.\n"
            "• 1.0 = a full interval late (a 10-day card ≥ 10 days late)\n"
            "• 0.8 = 80% of the interval late (a 10-day card ≥ 8 days late)\n"
            "• Higher = triggers less often (more lenient); lower = stricter."
        ),
    )
    min_days: int = Field(
        2,
        ge=0,
        le=3650,
        description=(
            "Safety floor in absolute days: a card is never treated as overdue until it is "
            "at least this many days late — regardless of the ratio.\n"
            "• Stops a short-interval card being capped for only a few hours or one "
            "day late.\n\n"
            "Example: min_days = 2 → a 1-day card that is 1 day late is left alone; it is "
            "only capped once it is ≥ 2 days late."
        ),
    )
    force_again_after_days: int = Field(
        7,
        ge=0,
        le=3650,
        title="Force overdue as again after days",
        description=(
            "Escalate to Again for very stale cards.\n"
            "After an overdue card is capped to Hard, if Hard would STILL schedule it more "
            "than this many days out, press Again instead.\n"
            "• A long-forgotten card fully resets into relearning instead of keeping a "
            "multi-day interval.\n"
            "• 0 = never escalate (always cap to Hard, never force Again)."
        ),
    )
