"""The level ladders: realm names on top of standard rating scales.

A level is a pure function of today's score — it rises and falls with it, the way a LeetCode or
Codeforces rating does. Each domain names its levels twice: the cultivation realm (Luyện Khí …
Chân Tiên) and the scale people already know.

* **English** — vocabulary size (word-equivalents). The first six realms are the CEFR levels at
  the X_Lex vocabulary-size bands of Milton (2009) / Milton & Alexiou: A1 < 1,500, A2 1,500,
  B1 2,500, B2 3,250, C1 3,750, C2 4,500. Above C2 the spec's coverage milestones continue the
  ladder: 8,000 (≈98% text coverage, Nation 2006), 14,000 (VST ceiling), 17,000 (educated native
  speakers, Goulden et al. 1990). Vocabulary is one component of CEFR, not the whole of it.
* **DSA** — Elo rating on the LeetCode scale, cut at the Codeforces tier boundaries.

Pure: no ``aqt``/``anki``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Optional

#: Realm names, index 0 = L1.
REALM_NAMES: tuple[str, ...] = (
    "Luyện Khí",
    "Trúc Cơ",
    "Kim Đan",
    "Nguyên Anh",
    "Hóa Thần",
    "Luyện Hư",
    "Hợp Thể",
    "Đại Thừa",
    "Chân Tiên",
)
LEVELS = len(REALM_NAMES)

#: Vocabulary-size thresholds per realm (word-equivalents); see the module docstring.
ENGLISH_THRESHOLDS: tuple[float, ...] = (
    0,
    1500,
    2500,
    3250,
    3750,
    4500,
    8000,
    14000,
    17000,
)
ENGLISH_LABELS: tuple[str, ...] = (
    "A1",
    "A2",
    "B1",
    "B2",
    "C1",
    "C2",
    "C2+",
    "C2+",
    "C2+",
)
#: Elo thresholds per realm (Codeforces tiers; L1 has no floor).
DSA_THRESHOLDS: tuple[float, ...] = (0, 1200, 1400, 1600, 1900, 2100, 2400, 2600, 3000)
DSA_LABELS: tuple[str, ...] = (
    "Newbie",
    "Pupil",
    "Specialist",
    "Expert",
    "Candidate Master",
    "Master",
    "Grandmaster",
    "International Grandmaster",
    "Legendary Grandmaster",
)


@dataclass(frozen=True)
class Level:
    """Where a score sits on a ladder, ready for the status card."""

    level: int
    realm: str
    label: str
    next_threshold: Optional[float]
    next_realm: Optional[str]
    next_label: Optional[str]
    progress: float


@dataclass(frozen=True)
class Ladder:
    """Nine increasing thresholds, each with its realm name and its standard-scale label.

    A score below the first threshold still holds L1 — L1 has no floor.
    """

    thresholds: tuple[float, ...]
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.thresholds) != LEVELS or len(self.labels) != LEVELS:
            raise ValueError(f"a ladder needs {LEVELS} thresholds and {LEVELS} labels")
        if any(b <= a for a, b in pairwise(self.thresholds)):
            raise ValueError("level thresholds must be strictly increasing")

    @classmethod
    def build(cls, thresholds: Sequence[float], labels: Sequence[str]) -> Ladder:
        """A ladder from config values."""
        return cls(tuple(float(t) for t in thresholds), tuple(labels))

    def held_by(self, score: float) -> int:
        """The highest level whose threshold ``score`` reaches (at least 1)."""
        return max(
            (i + 1 for i, t in enumerate(self.thresholds) if score >= t), default=1
        )

    def locate(self, score: float) -> Level:
        """Level, names and the share of the way to the next one (the progress bar)."""
        level = self.held_by(score)
        if level >= LEVELS:
            return Level(level, REALM_NAMES[-1], self.labels[-1], None, None, None, 1.0)
        low, high = self.thresholds[level - 1], self.thresholds[level]
        progress = (score - low) / (high - low)
        return Level(
            level=level,
            realm=REALM_NAMES[level - 1],
            label=self.labels[level - 1],
            next_threshold=high,
            next_realm=REALM_NAMES[level],
            next_label=self.labels[level],
            progress=max(0.0, min(1.0, progress)),
        )


def realm_name(level: int) -> str:
    """The realm name for a 1-based level."""
    return REALM_NAMES[max(1, min(LEVELS, level)) - 1]
