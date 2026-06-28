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
        description=(
            "How late counts as 'overdue'. A card is overdue when "
            "days-late ÷ scheduled-interval ≥ this. 0.8 ≈ 80% past due; "
            "1.0 = a full interval late. Higher = more lenient."
        ),
    )
    min_days: int = Field(
        2,
        ge=0,
        le=3650,
        description=(
            "A safety floor: never treat a card as overdue until it is at least this many "
            "days late, so short-interval cards aren't punished for being a few hours late."
        ),
    )
    force_again_after_days: int = Field(
        7,
        ge=0,
        le=3650,
        description=(
            "When an overdue card is capped to Hard but Hard would still schedule it more "
            "than this many days out, force Again instead so a long-forgotten card truly "
            "resets. 0 disables this."
        ),
    )
