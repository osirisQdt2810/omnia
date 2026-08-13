"""Tests for the promoted word-form seam (de-inflection + whole-word regex building).

These cases were ported from the word-lookup suite when the helpers moved to ``core/lang``;
that suite still exercises them through its re-export, which is what proves the promotion did
not change behaviour.
"""

from __future__ import annotations

import re

from omnia.core.lang.word_forms import (
    DEFAULT_DEINFLECTOR,
    Deinflector,
    word_boundary_pattern,
    word_variants,
    words_boundary_pattern,
)


class TestWordVariants:
    """Rule-based de-inflection: 'loved' must still find the card filed under 'love'."""

    def test_keeps_the_original_first(self):
        assert word_variants("loved")[0] == "loved"

    def test_regular_past_and_gerund(self):
        assert "love" in word_variants("loved")
        assert "love" in word_variants("loving")
        assert "look" in word_variants("looked")

    def test_doubled_consonant_is_undone(self):
        assert "stop" in word_variants("stopped")
        assert "run" in word_variants("running")

    def test_never_offers_an_impossible_e_form_for_a_doubled_stem(self):
        # "stoppe"/"runne" are not words; a doubled consonant means the base never had that e.
        assert "stoppe" not in word_variants("stopped")
        assert "runne" not in word_variants("running")

    def test_y_forms(self):
        assert "study" in word_variants("studies")
        assert "study" in word_variants("studied")
        assert "happy" in word_variants("happiest")

    def test_plural_and_adverb(self):
        assert "love" in word_variants("loves")
        assert "quick" in word_variants("quickly")
        assert "go" in word_variants("goes")

    def test_short_words_are_left_alone(self):
        # Stripping "as" -> "a" would match half the collection.
        assert word_variants("as") == ("as",)
        assert word_variants("is") == ("is",)

    def test_uninflected_word_yields_just_itself(self):
        assert word_variants("level") == ("level",)

    def test_blank_input(self):
        assert word_variants("   ") == ()
        assert word_variants("") == ()

    def test_is_capped(self):
        assert len(word_variants("studies")) <= 6

    def test_input_is_normalised(self):
        # Case and padding come from a double-click selection, not from a tidy caller.
        assert word_variants("  LOVED  ") == word_variants("loved")

    def test_candidates_are_deduped(self):
        variants = word_variants("looking")
        assert len(variants) == len(set(variants))

    def test_only_the_first_matching_rule_is_applied(self):
        # "ies" wins over the later "es"/"s" rules, so "studies" never offers "studie"/"studi".
        assert word_variants("studies") == ("studies", "study")

    def test_offers_both_replacements_in_rule_order(self):
        # "ed" -> ("", "e"): the bare stem first, then the e-restored form.
        assert word_variants("loved") == ("loved", "lov", "love")


class TestDeinflector:
    """The rule table and its thresholds are owned by an instance, not by module globals."""

    def test_default_instance_backs_the_module_function(self):
        assert DEFAULT_DEINFLECTOR.variants("stopped") == word_variants("stopped")

    def test_max_variants_caps_the_result(self):
        assert Deinflector(max_variants=1).variants("loved") == ("loved",)

    def test_min_inflected_guards_short_words(self):
        # Lowering the guard lets a 3-letter word be stripped, proving the threshold is what
        # keeps "as"/"is" intact rather than an accident of the rule table.
        assert Deinflector(min_inflected=3).variants("ads") == ("ads", "ad")

    def test_custom_rules_replace_the_table(self):
        forms = Deinflector(rules=(("ing", ("",)),))
        assert forms.variants("loved") == ("loved",)
        assert forms.variants("looking") == ("looking", "look")


class TestWordBoundaryPattern:
    """Whole-word matching: the middle ground between exact and substring."""

    def test_wraps_a_plain_word_in_boundaries(self):
        assert word_boundary_pattern("port") == r"(?i)\bport\b"

    def test_escapes_regex_metacharacters(self):
        # A term like "a.b" must not let "." match any character.
        assert word_boundary_pattern("a.b") == r"(?i)\ba\.b\b"

    def test_omits_a_boundary_that_could_never_match(self):
        # \b needs a word character beside it; "c++" ends in '+', so a trailing \b would make
        # the pattern match nothing at all.
        assert word_boundary_pattern("c++") == r"(?i)\bc\+\+"
        assert word_boundary_pattern("++c") == r"(?i)\+\+c\b"

    def test_multi_word_terms_are_supported(self):
        assert word_boundary_pattern("port of call").startswith(r"(?i)\bport")
        assert word_boundary_pattern("port of call").endswith(r"call\b")

    def test_is_case_insensitive(self):
        assert word_boundary_pattern("Port").startswith("(?i)")

    def test_blank_term_yields_no_pattern(self):
        assert word_boundary_pattern("   ") == ""

    def test_matches_the_word_but_not_a_longer_one(self):
        pattern = re.compile(word_boundary_pattern("port"))
        assert pattern.search("a port of call")
        assert pattern.search("PORT")
        assert not pattern.search("important")


class TestWordsBoundaryPattern:
    def test_boundary_is_dropped_only_on_the_side_that_cannot_carry_it(self):
        # "c++" starts with a word char (\b works) but ends with '+' (a trailing \b would make
        # the pattern match nothing), so only the RIGHT boundary is dropped.
        assert words_boundary_pattern(["c++"]) == r"(?i)\bc\+\+"
        assert words_boundary_pattern(["++c"]) == r"(?i)\+\+c\b"
        assert words_boundary_pattern(["go", "goes"]) == r"(?i)\b(?:go|goes)\b"

    def test_empty_and_blank_terms_are_ignored(self):
        assert words_boundary_pattern([]) == ""
        assert words_boundary_pattern(["", "  "]) == ""
        assert words_boundary_pattern(["", "go"]) == r"(?i)\bgo\b"

    def test_alternation_matches_any_variant(self):
        pattern = re.compile(words_boundary_pattern(word_variants("running")))
        assert pattern.search("He was running late")
        assert pattern.search("I run every day")
