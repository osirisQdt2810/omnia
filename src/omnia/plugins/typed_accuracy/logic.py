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

EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4


def accuracy_ratio(good: int, bad: int, missed: int) -> float:
    """Return correct/total character ratio in [0, 1] (0 when nothing was typed)."""
    total = good + bad + missed
    return good / total if total > 0 else 0.0


def decide_ease(ratio: float, threshold: float, pass_ease: str) -> Optional[int]:
    """Map an accuracy ``ratio`` to an Anki ease (or None to stage nothing).

    Args:
        ratio: accuracy in [0, 1] (0 for an empty / no-markup answer).
        threshold: pass cutoff.
        pass_ease: ``"good"``, ``"easy"`` or ``"no"`` — the ease used on a pass. ``"no"``
            stages no ease (the user's own press stands), while a fail still forces Hard.

    Returns:
        2 (Hard) on a fail; 4 (Easy) / 3 (Good) on a pass; None when ``pass_ease == "no"``
        and the answer passed (nothing is staged).
    """
    if ratio < threshold:
        return EASE_HARD
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
