"""Settings for the phrase corrector.

Deliberately small. Almost everything about a correction is decided by the phrase and the register
the user picked at the moment they asked; what is left here is what they would not want to answer
twice — which language the explanations come back in, and which model to spend.
"""

from __future__ import annotations

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.phrase_check.correction import MODES, WRITTEN


class PhraseCheckSettings(PersistedModel):
    """Settings for checking a phrase a clipper sent."""

    language: str = Field(
        default="English",
        title="Explain in",
        description=(
            "The language the reasons are written in. The phrase itself is always corrected as "
            "English — this is the language you think in, not the one being checked.\n"
            "\n"
            "A name rather than a code (``Vietnamese``, not ``vi``), because it goes into the "
            "prompt as-is and a model asked to write in ``vi-VN`` guesses at what that means. "
            "Codes are accepted and translated for the common ones."
        ),
    )
    default_mode: str = Field(
        default=WRITTEN,
        title="Check as",
        description=(
            "Which register to start in: ``written`` (essays, email) or ``spoken`` "
            "(conversation). The panel can switch per phrase; this is only where it opens.\n"
            "\n"
            "It matters more than it sounds: “I ain’t got none” is a mistake in writing and "
            "ordinary in speech, so a corrector with one standard is wrong half the time with "
            "total confidence."
        ),
        json_schema_extra={"choices": list(MODES)},
    )
    model: str = Field(
        default="",
        title="Model",
        description=(
            "Leave empty to use the model configured for Omnia. Set one to pin this feature to "
            "something cheaper or stronger than the rest of the add-on uses — corrections are "
            "short and frequent, which is a different trade-off from generating a whole card."
        ),
    )
    #: Where remembered corrections live. Not surfaced in the settings form — it is a cache, and
    #: a text box holding several hundred kilobytes of JSON is not a setting anybody edits.
    corrections: str = Field(default="", title="", description="", exclude=True)
