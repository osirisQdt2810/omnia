"""FSRS retrievability — how likely a card is to be recalled right now (spec §4.2).

``R(t, S) = (1 + factor · t / S) ** (-decay)`` with ``factor = 0.9 ** (-1/decay) - 1``, so
``R(S, S) = 0.9`` by the definition of stability. ``decay`` is the card's own FSRS-6 decay, or
0.5 (FSRS-4.5/5), which is what Anki itself falls back to.

Pure: no ``aqt``/``anki``; the collection reader hands rows in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

#: FSRS-4.5/5 decay — Anki's fallback when a card carries no "decay" of its own.
DEFAULT_DECAY = 0.5
SECONDS_PER_DAY = 86400


def retrievability(
    elapsed_days: float, stability: float, decay: float = DEFAULT_DECAY
) -> float:
    """``R(t, S)`` for one card; 0 for a card with no usable stability."""
    if stability <= 0:
        return 0.0
    decay = decay if decay > 0 else DEFAULT_DECAY
    factor = 0.9 ** (-1.0 / decay) - 1.0
    return float((1.0 + factor * max(0.0, elapsed_days) / stability) ** (-decay))


def parse_card_data(raw: Any) -> dict[str, Any]:
    """``cards.data`` as a dict — any key may be missing, and the column may be empty."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class CardMemory:
    """One card's memory state.

    Attributes:
        stability: FSRS stability in days (or the ``ivl`` fallback; see ``from_fsrs``).
        decay: The card's FSRS decay.
        last_review_s: Last review time in epoch seconds, or None for a never-reviewed card.
        from_fsrs: False when ``stability`` is the interval fallback — the window then
            recommends enabling FSRS, because that estimate is much coarser.
    """

    stability: float
    decay: float
    last_review_s: Optional[float]
    from_fsrs: bool = True

    @property
    def reviewed(self) -> bool:
        return self.last_review_s is not None

    def r(self, now_s: float) -> float:
        """Retrievability at ``now_s``; a new card is 0 (spec §4.2)."""
        if self.last_review_s is None:
            return 0.0
        return retrievability(
            (now_s - self.last_review_s) / SECONDS_PER_DAY, self.stability, self.decay
        )

    @classmethod
    def from_card(
        cls,
        data: dict[str, Any],
        *,
        ivl: int,
        last_review_s: Optional[float],
    ) -> CardMemory:
        """Build from ``cards.data`` + the revlog's last review.

        ``lrt`` (seconds) is preferred; without it the last revlog entry is used, which is
        exact rather than Anki's ``due - ivl`` estimate. Without FSRS stability, ``S ≈ ivl``
        [HEURISTIC, spec §4.2].
        """
        lrt = data.get("lrt")
        last = (
            float(lrt) if isinstance(lrt, (int, float)) and lrt > 0 else last_review_s
        )
        stability = data.get("s")
        decay = data.get("decay")
        decay = (
            float(decay)
            if isinstance(decay, (int, float)) and decay > 0
            else DEFAULT_DECAY
        )
        if isinstance(stability, (int, float)) and stability > 0:
            return cls(float(stability), decay, last, True)
        return cls(float(max(ivl, 0)), decay, last, False)
