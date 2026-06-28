"""Pure typing-accuracy logic (no Anki imports — unit-testable).

Anki marks a typed answer with ``.typeGood`` / ``.typeBad`` / ``.typeMissed`` spans; the
accuracy ratio is correct characters over total. The ratio maps to an ease: a pass uses the
configured ease (Good/Easy), a fail uses Hard.
"""

from __future__ import annotations

EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4


def accuracy_ratio(good: int, bad: int, missed: int) -> float:
    """Return correct/total character ratio in [0, 1] (0 when nothing was typed)."""
    total = good + bad + missed
    return good / total if total > 0 else 0.0


def decide_ease(ratio: float, threshold: float, pass_ease: str) -> int:
    """Map an accuracy ``ratio`` to an Anki ease.

    Args:
        ratio: accuracy in [0, 1].
        threshold: pass cutoff.
        pass_ease: ``"good"`` or ``"easy"`` — the ease used on a pass.

    Returns:
        4 (Easy) or 3 (Good) on a pass; 2 (Hard) on a fail.
    """
    if ratio >= threshold:
        return EASE_EASY if pass_ease == "easy" else EASE_GOOD
    return EASE_HARD
