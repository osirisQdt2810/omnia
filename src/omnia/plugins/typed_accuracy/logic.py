"""Pure typing-accuracy logic (no Anki imports — unit-testable).

Anki marks a typed answer with ``.typeGood`` / ``.typeBad`` / ``.typeMissed`` spans; the
accuracy ratio is correct characters over total. The ratio maps to an ease, mirroring the
reference's ``decide()``: below the threshold (including an empty / no-markup answer) forces
Hard; at/above the threshold the configured pass ease is applied — except ``"no"``, which
stages nothing so the user's own press stands while the result is still logged.

The result code (good/bad/miss/empty) is what gets logged to the stats store; it is decided
from which markup spans are present, independent of the ease decision.
"""

from __future__ import annotations

from typing import Optional

from omnia.plugins.typed_accuracy.store import (
    RESULT_BAD,
    RESULT_EMPTY,
    RESULT_GOOD,
    RESULT_MISS,
)

EASE_AGAIN = 1
EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4

#: What a FAILING answer may be graded as, and the ease each name means.
#:
#: ``"no"`` is in both this and the pass map for the same reason: staging nothing is a real
#: answer to "what should happen", not the absence of one — the user's own key press stands.
FAIL_EASES: dict[str, Optional[int]] = {
    "again": EASE_AGAIN,
    "hard": EASE_HARD,
    "no": None,
}


def accuracy_ratio(good: int, bad: int, missed: int) -> float:
    """Return correct/total character ratio in [0, 1] (0 when nothing was typed)."""
    total = good + bad + missed
    return good / total if total > 0 else 0.0


def decide_ease(
    ratio: float, threshold: float, pass_ease: str, fail_ease: str = "hard"
) -> Optional[int]:
    """Map an accuracy ``ratio`` to an Anki ease (or None to stage nothing).

    Both sides of the threshold are the user's to choose. A fail used to force Hard with no way
    to say otherwise, which suits a forgiving deck and not a strict one: someone drilling
    spelling wants a miss to go straight back into the queue (Again), and someone using typing
    as a hint wants their own press to stand (no).

    Args:
        ratio: accuracy in [0, 1] (0 for an empty / no-markup answer).
        threshold: pass cutoff.
        pass_ease: ``"good"``, ``"easy"`` or ``"no"`` — the ease used on a pass.
        fail_ease: ``"again"``, ``"hard"`` or ``"no"`` — the ease used on a fail. Defaults to
            ``"hard"``, which is what this did before it was configurable.

    Returns:
        The ease to stage, or ``None`` to stage nothing (the user's own press stands). An
        unrecognised name falls back to the default for its side rather than staging nothing:
        a grader that quietly stops grading is harder to notice than one that grades plainly.
    """
    if ratio < threshold:
        return FAIL_EASES.get(fail_ease, EASE_HARD)
    if pass_ease == "easy":
        return EASE_EASY
    if pass_ease == "no":
        return None
    return EASE_GOOD


def result_code(has_good: bool, has_bad: bool, has_miss: bool) -> int:
    """Map the presence of typed-answer markup spans to a result code.

    Mirrors the reference's precedence (miss > bad > good); when no span is present the
    answer is treated as empty.
    """
    if has_miss:
        return RESULT_MISS
    if has_bad:
        return RESULT_BAD
    if has_good:
        return RESULT_GOOD
    return RESULT_EMPTY
