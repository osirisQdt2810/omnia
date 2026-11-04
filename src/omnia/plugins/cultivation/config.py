"""Cultivation settings (the plugin's own Pydantic v1 config).

The spec's rule is that no number is invented in code: every constant is either backed by a
reference or tagged [HEURISTIC] and kept here, where the user can calibrate it. The defaults are
the spec's values (CONTEXT.md §3); the guidance doc lists which ones are heuristics.

Layout:

* Flat scalars — rendered by the generic settings form (feed path, buffers, cooldowns).
* ``anki`` / ``dsa`` — nested tuning tables, edited in ``features.toml``; the generic form skips
  nested models on purpose (a 30-slider form is not a settings page anyone reads).
* ``decks`` / ``word_fields`` / ``meaning_fields`` / ``modality`` — written by the Cultivation
  window's *Setup* tab, which lists the real decks, note types and templates to pick from.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from omnia.core.config.base import PersistedModel

#: Modalities in increasing strength of evidence (spec §4.3; Laufer & Goldstein 2004).
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
    """Anki-domain constants (spec §4)."""

    # μ per modality — ordering from the literature, magnitudes [HEURISTIC].
    weight_recognition: float = 0.6
    weight_passive_recall: float = 0.8
    weight_active_recall: float = 0.9
    weight_active_recall_typed: float = 1.0
    # Trial (§4.6): n = 50, pass ≥ 43 (binomial operating characteristic around 85% mastery).
    trial_size: int = 50
    trial_pass: int = 43
    trial_recent_hours: int = (
        24  # words reviewed this recently are excluded (short-term memory)
    )
    trial_new_share: float = (
        0.5  # share sampled from words first reviewed since the last promotion
    )
    typo_min_length: int = (
        7  # [HEURISTIC] Damerau–Levenshtein 1 accepted from this length up
    )
    # Health band (§4.7): FSRS DR 0.9; ~85% optimal accuracy (Wilson et al. 2019).
    retention_low: float = 0.80
    retention_high: float = 0.95
    retention_window_days: int = 30

    def weights(self) -> dict[str, float]:
        """μ keyed by modality."""
        return {
            RECOGNITION: self.weight_recognition,
            PASSIVE_RECALL: self.weight_passive_recall,
            ACTIVE_RECALL: self.weight_active_recall,
            ACTIVE_RECALL_TYPED: self.weight_active_recall_typed,
        }


class DsaTuning(PersistedModel):
    """DSA-domain constants (spec §5)."""

    initial_rating: float = 1500.0  # LeetCode's starting rating, or your contest rating
    # Quality score s by status (§5.3) — values [HEURISTIC].
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
    # Neglect decay (§5.4) [HEURISTIC, calibrate].
    decay_grace_days: int = 7
    decay_half_life_days: float = 90.0
    decay_floor_keep: float = 0.5
    decay_floor_base: float = 1200.0
    # Trial (§5.6).
    trial_size: int = 5
    trial_pass: int = 3
    trial_window: float = 100.0  # problems rated within ±window of the target threshold
    trial_min_contests: int = 2
    trial_days: int = 14


class CultivationSettings(PersistedModel):
    """Settings for the Cultivation feature."""

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
    demotion_buffer_days: int = Field(
        14,
        ge=1,
        le=90,
        title="Days below a realm before dropping",
        description="[HEURISTIC] A realm only drops after this many days below its threshold.",
    )
    trial_cooldown_days: int = Field(
        7,
        ge=0,
        le=60,
        title="Cooldown after a failed trial (days)",
        description="[HEURISTIC] Time for the corrective practice before the next attempt.",
    )
    decks: list[str] = Field(
        default_factory=list,
        description="Deck names (and their subdecks) holding vocabulary; empty = all decks.",
    )
    word_fields: dict[str, str] = Field(
        default_factory=dict,
        description="Note type -> the field holding the English word.",
    )
    meaning_fields: dict[str, str] = Field(
        default_factory=dict,
        description="Note type -> the field holding its meaning (used by the trial).",
    )
    modality: dict[str, str] = Field(
        default_factory=dict,
        description='"Note type/Template" -> modality, confirmed in the Setup tab.',
    )
    anki: AnkiTuning = Field(default_factory=AnkiTuning)
    dsa: DsaTuning = Field(default_factory=DsaTuning)

    def feed_path(self) -> Path:
        """The DSA feed file to read."""
        raw = (self.dsa_feed_path or "").strip()
        return Path(raw).expanduser() if raw else DEFAULT_FEED
