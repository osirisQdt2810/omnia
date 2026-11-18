"""The level ladders: realm names on CEFR / Elo thresholds. No Anki anywhere."""

from __future__ import annotations

import pytest

from omnia.plugins.cultivation.config import AnkiTuning, DsaTuning
from omnia.plugins.cultivation.realms import (
    DSA_LABELS,
    ENGLISH_LABELS,
    Ladder,
    realm_name,
)

ENGLISH = AnkiTuning().ladder()
DSA = DsaTuning().ladder()


class TestEnglish:
    @pytest.mark.parametrize(
        "words,label,realm",
        [
            (0, "A1", "Luyện Khí"),
            (1499.9, "A1", "Luyện Khí"),
            (1500, "A2", "Trúc Cơ"),
            (2731, "B1", "Kim Đan"),
            (3250, "B2", "Nguyên Anh"),
            (3800, "C1", "Hóa Thần"),
            (4500, "C2", "Luyện Hư"),
            (9000, "C2+", "Hợp Thể"),
            (20000, "C2+", "Chân Tiên"),
        ],
    )
    def test_cefr_bands_follow_milton(self, words, label, realm):
        level = ENGLISH.locate(words)
        assert (level.label, level.realm) == (label, realm)

    def test_progress_toward_the_next_level(self):
        # B1 runs 2,500 → 3,250, so 2,875 is half way to B2.
        level = ENGLISH.locate(2875)
        assert level.progress == pytest.approx(0.5)
        assert (level.next_label, level.next_threshold) == ("B2", 3250)

    def test_the_level_falls_with_the_score(self):
        assert ENGLISH.held_by(2600) == 3
        assert ENGLISH.held_by(2400) == 2


class TestDsa:
    @pytest.mark.parametrize(
        "rating,label",
        [
            (800, "Newbie"),
            (1200, "Pupil"),
            (1500, "Specialist"),
            (1640, "Expert"),
            (1950, "Candidate Master"),
            (2200, "Master"),
            (3100, "Legendary Grandmaster"),
        ],
    )
    def test_codeforces_tiers(self, rating, label):
        assert DSA.locate(rating).label == label

    def test_a_score_below_the_first_threshold_is_still_level_one(self):
        level = DSA.locate(-50)
        assert level.level == 1 and level.progress == 0.0

    def test_the_top_level_has_nothing_above(self):
        level = DSA.locate(3500)
        assert level.next_threshold is None and level.progress == 1.0


def test_a_ladder_must_increase():
    with pytest.raises(ValueError):
        Ladder.build([0, 1, 1, 2, 3, 4, 5, 6, 7], ENGLISH_LABELS)
    with pytest.raises(ValueError):
        Ladder.build([0, 1, 2], DSA_LABELS)


def test_names():
    assert realm_name(1) == "Luyện Khí"
    assert realm_name(9) == "Chân Tiên"
