"""Cultivation settings (the plugin's own Pydantic v1 config).

No number is invented in code: every constant is either backed by a reference or tagged
[HEURISTIC] and kept here, where it can be calibrated. The guidance doc lists which is which.

Layout:

* Flat scalars — rendered by the generic settings form (start date, feed path, suspended cards).
* ``anki`` / ``dsa`` — nested tuning tables, edited in ``features.toml``; the generic form skips
  nested models on purpose.

There is no deck, field or card-type setting on purpose: every deck counts, and the plugin works
out each note's word field and each card type's weight by itself (see :mod:`.reader`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.cultivation.realms import (
    DSA_LABELS,
    DSA_THRESHOLDS,
    ENGLISH_LABELS,
    ENGLISH_THRESHOLDS,
    Ladder,
)

#: Modalities in increasing strength of evidence (Laufer & Goldstein 2004).
RECOGNITION = "recognition"
PASSIVE_RECALL = "passive_recall"
ACTIVE_RECALL = "active_recall"
ACTIVE_RECALL_TYPED = "active_recall_typed"
MODALITIES: tuple[str, ...] = (
    RECOGNITION,
    PASSIVE_RECALL,
    ACTIVE_RECALL,
    ACTIVE_RECALL_TYPED,
)

DEFAULT_FEED = Path.home() / ".omnia" / "cultivation" / "dsa_feed.json"


class AnkiTuning(PersistedModel):
    """English-domain constants."""

    # μ per modality — ordering from the literature, magnitudes [HEURISTIC].
    weight_recognition: float = 0.6
    weight_passive_recall: float = 0.8
    weight_active_recall: float = 0.9
    weight_active_recall_typed: float = 1.0
    # Health band: FSRS desired retention 0.9; ~85% optimal accuracy (Wilson et al. 2019).
    retention_low: float = 0.80
    retention_high: float = 0.95
    retention_window_days: int = 30
    # Level thresholds in word-equivalents: CEFR bands (Milton 2009) then coverage milestones.
    level_thresholds: list[float] = Field(
        default_factory=lambda: list(ENGLISH_THRESHOLDS)
    )

    def weights(self) -> dict[str, float]:
        """μ keyed by modality."""
        return {
            RECOGNITION: self.weight_recognition,
            PASSIVE_RECALL: self.weight_passive_recall,
            ACTIVE_RECALL: self.weight_active_recall,
            ACTIVE_RECALL_TYPED: self.weight_active_recall_typed,
        }

    def ladder(self) -> Ladder:
        return Ladder.build(self.level_thresholds, ENGLISH_LABELS)


class DsaTuning(PersistedModel):
    """DSA-domain constants."""

    initial_rating: float = 1500.0  # LeetCode's starting rating, or your contest rating
    # Quality score s by status — values [HEURISTIC].
    s_self_fast: float = 1.0
    s_self: float = 0.8
    s_hint: float = 0.5
    s_editorial: float = 0.2
    s_copied: float = 0.0
    legacy_unknown_s: float = 0.7  # [HEURISTIC]
    legacy_unknown_weight: float = 0.5  # [HEURISTIC]
    fast_max_wa: int = (
        2  # "self" counts as fast only with at most this many wrong submissions
    )
    # Time limits by problem rating [HEURISTIC; ~LeetCode contest pace].
    time_limit_easy_below: float = 1400.0
    time_limit_hard_from: float = 1900.0
    time_limit_easy_min: int = 20
    time_limit_medium_min: int = 40
    time_limit_hard_min: int = 60
    # Unrated problems by LeetCode difficulty [HEURISTIC; calibrate against zerotrac medians].
    fallback_easy: float = 1200.0
    fallback_medium: float = 1600.0
    fallback_hard: float = 2000.0
    # K-factor (FIDE): 40 for the first 30 rated attempts, then 20; 10 once θ ever reached 2400.
    k_new: float = 40.0
    k_new_attempts: int = 30
    k_normal: float = 20.0
    k_master: float = 10.0
    k_master_from: float = 2400.0
    # Post-break boost [HEURISTIC]: K = 40 for 10 attempts after a gap of ≥ 30 days.
    boost_gap_days: int = 30
    boost_attempts: int = 10
    # Re-solve / debt spacing (Cepeda et al. 2008).
    resolve_gap_days: int = 7
    resolve_weight: float = 0.5
    debt_warn_days: int = 30
    # Neglect decay [HEURISTIC, calibrate].
    decay_grace_days: int = 7
    decay_half_life_days: float = 90.0
    decay_floor_keep: float = 0.5
    decay_floor_base: float = 1200.0
    # Level thresholds on the Elo scale (Codeforces tiers).
    level_thresholds: list[float] = Field(default_factory=lambda: list(DSA_THRESHOLDS))

    def ladder(self) -> Ladder:
        return Ladder.build(self.level_thresholds, DSA_LABELS)


class CultivationSettings(PersistedModel):
    """Settings for the Cultivation feature."""

    start_date: str = Field(
        "",
        title="Counting since (YYYY-MM-DD)",
        description=(
            "Only words reviewed and problems solved from this day on count.\n"
            "• Set to the day you switch Cultivation on; 'Start over' in the window resets it.\n"
            "• Move it back to count older history too."
        ),
    )
    dsa_feed_path: str = Field(
        "",
        title="DSA feed file",
        description=(
            "The JSON file dsa-journey's /commit-progress writes after every run.\n"
            f"• Empty = the default, {DEFAULT_FEED}.\n"
            "• Must match OMNIA_CULTIVATION_FEED if you set that for the exporter."
        ),
    )
    include_suspended: bool = Field(
        True,
        title="Count suspended and buried cards",
        description="They still decay, so they still count — unless you untick this.",
    )
    anki: AnkiTuning = Field(default_factory=AnkiTuning)
    dsa: DsaTuning = Field(default_factory=DsaTuning)

    def feed_path(self) -> Path:
        """The DSA feed file to read."""
        raw = (self.dsa_feed_path or "").strip()
        return Path(raw).expanduser() if raw else DEFAULT_FEED

    def start(self) -> Optional[date]:
        """The counting start day, or None when unset or unreadable."""
        try:
            return date.fromisoformat(self.start_date.strip())
        except ValueError:
            return None
