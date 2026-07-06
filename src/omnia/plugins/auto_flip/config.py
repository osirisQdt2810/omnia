"""Auto-flip settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin so the feature owns its schema; the generic settings form is
derived from it via :func:`omnia.core.config.schema.schema_from_model`. Field descriptions
become GUI tooltips and ``ge``/``le`` bounds drive the numeric widgets. The ``per_deck``
override map is edited by the bespoke deck-options dialog, not the generic form, so it is
skipped by the schema deriver.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class AutoFlipDeckOverride(_Strict):
    """Per-deck auto-flip override (keyed by deck id in :class:`AutoFlipSettings`).

    Mirrors the reference add-on's two-flag deck gate (``use_general`` / ``use_deck``):

    * ``use_global=True`` → the deck has an override row but defers to the global delays
      (the reference's ``use_general``); the per-deck delays below are ignored.
    * ``use_global=False`` + ``enabled=True`` → use this row's delays (``use_deck``).
    * ``enabled=False`` → auto-flip is OFF for this deck (``use_deck=False``), regardless of
      ``use_global``.
    """

    use_global: bool = False
    enabled: bool = True
    delay_question_seconds: float = Field(3.0, ge=0)
    delay_answer_seconds: float = Field(3.0, ge=0)


class AutoFlipSettings(_Strict):
    """Settings for the auto-flip feature."""

    delay_question_seconds: float = Field(
        3.0,
        ge=0,
        le=120,
        description=(
            "How long the question side stays up before Omnia auto-flips it to the answer.\n"
            "• In seconds (0 = flip as soon as the question renders).\n"
            "• Per-deck overrides take precedence (deck gear menu → Auto-Flip…)."
        ),
    )
    delay_answer_seconds: float = Field(
        3.0,
        ge=0,
        le=120,
        description=(
            "How long the answer side stays up before Omnia auto-grades Good and advances.\n"
            "• In seconds (0 = grade as soon as the answer renders).\n"
            "• Press Enter first to cancel the pending auto-grade and take over manually."
        ),
    )
    wait_for_audio: bool = Field(
        True,
        description=(
            "Wait for the card's audio to finish before starting the countdown.\n"
            "• On: the countdown begins only once all of the side's sounds have played.\n"
            "• Off: the countdown starts as soon as the side is shown.\n"
            "• Keeps a card from flipping before you have heard it."
        ),
    )
    show_timer: bool = Field(
        True,
        description=(
            "Show the shrinking countdown ring in the corner while a flip/grade is pending.\n"
            "• On: a visual ring counts down to the auto action.\n"
            "• Off: the timer still runs, just without the on-screen ring."
        ),
    )
    # deck id (as a string) -> override; empty means "use the global delays everywhere".
    per_deck: dict[str, AutoFlipDeckOverride] = Field(default_factory=dict)
