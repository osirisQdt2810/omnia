"""Typing-accuracy settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin; the generic settings form is derived from it via
:func:`omnia.core.config.schema.schema_from_model`. Field descriptions become GUI tooltips
and ``ge``/``le`` bounds drive the numeric widgets.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from omnia.core.config.base import PersistedModel

# Omitted from the serialized form while never set — see TypedAccuracySettings.dict. Listed
# once so the prune cannot drift from the set it covers.
_PRUNE_WHILE_UNSET = ("high_threshold", "high_ease")


class TypedAccuracySettings(PersistedModel):
    """Settings for the typing-accuracy grader."""

    threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        # The two marks cut the SAME axis, so the settings page draws them as one track with
        # two handles rather than two sliders whose relationship the reader has to reconstruct.
        upper_key="high_threshold",
        description=(
            "Fraction of the typed answer that must be correct to count as a pass.\n"
            "• 0.7 = 70% of characters right.\n"
            "• At or above this → the pass ease is staged.\n"
            "• Below this → the fail ease is staged."
        ),
    )
    # The optional SECOND cutoff, and the reason the first one is not enough: one line through
    # the ratio can only say right or wrong, and a typed answer is not that binary. "Nearly
    # perfect" and "scraped a pass" are different recalls and deserve different intervals.
    #
    # Zero means OFF — one cutoff, exactly as before — and so does any value at or below
    # ``threshold``, because a top band that starts at or under the pass mark is not a band.
    # Degrading rather than rejecting is deliberate: this is a SYNCED value, so a nonsensical
    # pair can arrive from another device or another release, and a grader that refuses to
    # load is worse than one that grades the way it always did.
    high_threshold: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Optional second cutoff, above the pass one, for a nearly-perfect answer.\n"
            "• 0 = off: one cutoff, pass or fail.\n"
            "• 0.95 with a 0.8 pass mark: under 0.8 fails, 0.8–0.95 gets the pass ease, "
            "0.95 and up gets the high ease.\n"
            "• At or below the pass mark it is ignored, since the band would be empty."
        ),
    )
    high_ease: Literal["good", "easy", "no"] = Field(
        "easy",
        description=(
            "Which ease to auto-stage for an answer at or above the second cutoff.\n"
            "• Ignored while that cutoff is 0.\n"
            "• no: stage nothing — your own key press stands."
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

    def dict(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize, omitting the second-cutoff keys while they have never been set.

        These settings live in the SYNCED collection blob, so a key written here reaches every
        device the profile touches — including ones on releases that predate the key. Omitting
        them keeps the stored form byte-identical for anybody who has not turned the second
        band on, which is the same discipline ``SmartNotesSettings`` applies to its own
        never-set keys.

        "Never set", not "equal to the default": a user who deliberately picks ``easy`` for the
        high band, when ``easy`` is already the default, has still made a choice — and would
        lose it on the next save if the prune were keyed on the value.

        Args:
            **kwargs: Passed through to :meth:`pydantic.BaseModel.dict` unchanged.

        Returns:
            The settings' serialized form, without the never-set second-cutoff keys.
        """
        data: dict[str, Any] = super().dict(**kwargs)
        for key in _PRUNE_WHILE_UNSET:
            if key not in self.__fields_set__:
                data.pop(key, None)
        return data
