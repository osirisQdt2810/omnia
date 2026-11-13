"""Retrievability, words and vocabulary power (spec §4.2–§4.4, §4.8). No Anki anywhere."""

from __future__ import annotations

import pytest

from omnia.plugins.cultivation.config import (
    ACTIVE_RECALL,
    ACTIVE_RECALL_TYPED,
    PASSIVE_RECALL,
    AnkiTuning,
)
from omnia.plugins.cultivation.memory import (
    SECONDS_PER_DAY,
    CardMemory,
    parse_card_data,
    retrievability,
)
from omnia.plugins.cultivation.vocab import (
    VocabCard,
    VocabularyPower,
    group_words,
    normalize_word,
    start_of_day_states,
    suggest_modality,
    suggest_word_field,
)

NOW = 1_800_000_000.0
DAY = SECONDS_PER_DAY


class TestRetrievability:
    @pytest.mark.parametrize("decay", [0.5, 0.2, 0.1542])
    def test_r_at_t_equal_s_is_ninety_percent(self, decay):
        """The definition of stability: R(S, S) = 0.9 for any decay."""
        for s in (0.5, 3.0, 40.0, 365.0):
            assert retrievability(s, s, decay) == pytest.approx(0.9, abs=1e-12)

    def test_factor_is_19_over_81_at_the_default_decay(self):
        # R(t,S) = (1 + 19/81 · t/S)^-0.5
        assert retrievability(10, 5) == pytest.approx((1 + 19 / 81 * 2) ** -0.5)

    def test_r_is_one_right_after_review_and_falls_with_time(self):
        assert retrievability(0, 10) == 1.0
        assert retrievability(5, 10) > retrievability(50, 10) > 0

    def test_no_stability_means_no_recall(self):
        assert retrievability(1, 0) == 0.0

    def test_a_new_card_is_zero(self):
        assert CardMemory(10, 0.5, None).r(NOW) == 0.0


class TestCardData:
    def test_fsrs_state_and_lrt_are_read(self):
        data = parse_card_data(
            '{"s": 12.5, "d": 5.1, "dr": 0.9, "decay": 0.2, "lrt": 1700000000}'
        )
        mem = CardMemory.from_card(data, ivl=3, last_review_s=1600000000.0)
        assert (mem.stability, mem.decay, mem.last_review_s, mem.from_fsrs) == (
            12.5,
            0.2,
            1700000000.0,
            True,
        )

    def test_missing_keys_fall_back_to_revlog_and_interval(self):
        mem = CardMemory.from_card(
            parse_card_data(""), ivl=21, last_review_s=1600000000.0
        )
        assert (mem.stability, mem.decay, mem.last_review_s, mem.from_fsrs) == (
            21.0,
            0.5,
            1600000000.0,
            False,
        )

    def test_garbage_data_is_an_empty_dict(self):
        assert parse_card_data("{not json") == {}
        assert parse_card_data("[1, 2]") == {}


class TestWords:
    def test_normalize_strips_html_sound_and_case(self):
        assert normalize_word("<b>Ephemeral</b>&nbsp; [sound:x.mp3]") == "ephemeral"

    def test_modality_suggestions(self):
        assert (
            suggest_modality("{{Meaning}}<br>{{type:Word}}", "", "Word")
            == ACTIVE_RECALL_TYPED
        )
        assert suggest_modality("{{Word}}", "{{Meaning}}", "Word") == PASSIVE_RECALL
        assert suggest_modality("{{text:Word}}", "", "Word") == PASSIVE_RECALL
        assert suggest_modality("{{Meaning}}", "{{Word}}", "Word") == ACTIVE_RECALL
        # A conditional section is not the word being shown.
        assert (
            suggest_modality("{{#Word}}x{{/Word}}{{Meaning}}", "", "Word")
            == ACTIVE_RECALL
        )

    def test_field_suggestions(self):
        fields = ["Front", "Back", "Audio"]
        assert suggest_word_field(fields) == "Front"
        assert suggest_word_field(["Vocab EN", "Nghĩa"]) == "Vocab EN"


def card(cid, word, modality, s=10.0, days_ago=10.0, **kw) -> VocabCard:
    last = None if days_ago is None else NOW - days_ago * DAY
    return VocabCard(cid, word, modality, CardMemory(s, 0.5, last), **kw)


WEIGHTS = AnkiTuning().weights()


class TestPower:
    def test_power_equals_a_hand_computed_sum_of_mu_times_r(self):
        """Spec M1 acceptance: power = Σ_w max μ·R, checked by hand to 1e-6."""
        cards = [
            card(1, "apple", PASSIVE_RECALL, s=10, days_ago=10),
            card(2, "apple", ACTIVE_RECALL_TYPED, s=4, days_ago=10),
            card(3, "brave", ACTIVE_RECALL, s=30, days_ago=3),
            card(4, "candid", PASSIVE_RECALL, s=5, days_ago=None),  # new
        ]
        r = lambda t, s: (1 + 19 / 81 * t / s) ** -0.5  # noqa: E731
        expected = max(0.8 * r(10, 10), 1.0 * r(10, 4)) + 0.9 * r(3, 30)
        power = VocabularyPower(WEIGHTS).power(group_words(cards), NOW)
        assert power == pytest.approx(expected, abs=1e-6)

    def test_notes_with_the_same_word_are_merged(self):
        words = group_words(
            [card(1, "run", PASSIVE_RECALL), card(2, "run", ACTIVE_RECALL)]
        )
        assert list(words) == ["run"]
        assert len(words["run"].cards) == 2

    def test_a_weight_above_nothing_never_exceeds_one_word(self):
        words = group_words([card(1, "x", ACTIVE_RECALL_TYPED, days_ago=0)])
        assert VocabularyPower(WEIGHTS).power(words, NOW) == pytest.approx(1.0)

    def test_only_cards_reviewed_since_the_start_count(self):
        """A fresh start begins at zero: old reviews add nothing until the word is reviewed."""
        old = card(1, "old", PASSIVE_RECALL, days_ago=30)
        new = card(2, "new", PASSIVE_RECALL, days_ago=1)
        words = group_words([old, new])
        since = NOW - 2 * DAY
        power = VocabularyPower(WEIGHTS, since_s=since).power(words, NOW)
        assert power == pytest.approx(0.8 * new.memory.r(NOW))
        assert not words["old"].counted(since) and words["new"].counted(since)

    def test_a_start_after_every_review_scores_zero(self):
        words = group_words([card(1, "a", PASSIVE_RECALL, days_ago=3)])
        assert VocabularyPower(WEIGHTS, since_s=NOW).power(words, NOW) == 0.0

    def test_baseline_is_added(self):
        assert VocabularyPower(WEIGHTS, baseline=250).power({}, NOW) == 250


class TestStartOfDay:
    def test_cards_untouched_today_keep_their_state(self):
        c = card(1, "a", PASSIVE_RECALL, days_ago=3)
        states = start_of_day_states([c], NOW - 3600, {})
        assert states[1] == c.memory

    def test_cards_reviewed_today_take_the_snapshot(self):
        c = card(1, "a", PASSIVE_RECALL, s=20, days_ago=0.01)
        old = CardMemory(8, 0.5, NOW - 5 * DAY)
        assert start_of_day_states([c], NOW - 3600, {1: old})[1] == old

    def test_a_card_first_learnt_today_started_from_nothing(self):
        c = card(1, "a", PASSIVE_RECALL, days_ago=0.01)
        assert start_of_day_states([c], NOW - 3600, {})[1].reviewed is False

    def test_gain_isolates_todays_reviews_from_overnight_decay(self):
        """A review today raises S; the gain is power(now, after) - power(now, before)."""
        before = CardMemory(5, 0.5, NOW - 6 * DAY)
        after = card(1, "a", PASSIVE_RECALL, s=15, days_ago=0.01)
        model = VocabularyPower(WEIGHTS)
        words = group_words([after])
        gain = model.power(words, NOW) - model.power(
            words, NOW, start_of_day_states([after], NOW - 3600, {1: before})
        )
        assert gain == pytest.approx(0.8 * (after.memory.r(NOW) - before.r(NOW)))
        assert gain > 0
