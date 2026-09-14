"""Settings for the phrase corrector.

Deliberately small. Almost everything about a correction is decided by the phrase and the register
the user picked at the moment they asked; what is left here is what they would not want to answer
twice — which language the explanations come back in, and which model to spend.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.phrase_check.correction import WRITTEN


class PhraseCheckSettings(PersistedModel):
    """Settings for checking a phrase a clipper sent.

    Settings only. The cache of remembered corrections deliberately lives in a file under
    ``user_files/`` rather than here — it is per-machine scratch, and this domain syncs.
    """

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
    # ``Literal``, not ``str`` + a choices hint. It both validates the value and drives the
    # generic form's dropdown (core/config/schema.py reads the annotation, and nothing else).
    # Spelled out rather than ``Literal[SPOKEN, WRITTEN]``, because mypy requires literal values
    # there and rejects the names — so ``test_wiring`` pins these against ``correction.MODES``,
    # which stays the single source of truth everywhere else.
    # The v2 spelling of that hint — ``json_schema_extra`` — is swallowed without error by the
    # Pydantic 1.10 this add-on vendors, so the field rendered as a free-text box: a user could
    # type "speech", have it save without complaint, and get every correction judged in the
    # wrong register with nothing on screen saying so. Which is exactly the failure the two
    # registers exist to prevent, reintroduced through the settings dialog.
    default_mode: Literal["spoken", "written"] = Field(
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
