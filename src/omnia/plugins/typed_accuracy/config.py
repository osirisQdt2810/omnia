"""Typing-accuracy settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin; the generic settings form is derived from it via
:func:`omnia.core.config.schema.schema_from_model`. Field descriptions become GUI tooltips
and ``ge``/``le`` bounds drive the numeric widgets.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from omnia.core.config.base import PersistedModel


class TypedAccuracySettings(PersistedModel):
    """Settings for the typing-accuracy grader."""

    threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the typed answer that must be correct to count as a pass.\n"
            "• 0.7 = 70% of characters right.\n"
            "• At or above this → the pass ease is staged.\n"
            "• Below this → the fail ease is staged."
        ),
    )
    # Auto-answer on a pass: "good"/"easy" stage that ease; "no" stages nothing (the user's
    # own press stands). ``Literal`` both validates the value and drives the settings form's
    # choice widget.
    pass_ease: Literal["good", "easy", "no"] = Field(
        "good",
        description=(
            "Which ease to auto-stage when the typed answer passes.\n"
            "• good / easy: stage that grade for you on a pass.\n"
            "• no: stage nothing — your own key press stands."
        ),
    )
    # The other side of the threshold, and it was hard-coded to Hard until now. Default kept at
    # "hard" so nobody's grading changes by upgrading.
    fail_ease: Literal["again", "hard", "no"] = Field(
        "hard",
        description=(
            "Which ease to auto-stage when the typed answer fails.\n"
            "• again: send it straight back into the queue — strict, for spelling drills.\n"
            "• hard: keep it in rotation but set it back. The default.\n"
            "• no: stage nothing — your own key press stands.\n"
            "\n"
            "How wrong a typo should count depends on what you are drilling, which is why "
            "this is a choice and not a rule. A missed accent in a language deck is not the "
            "same mistake as a misspelt term you are being examined on."
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
