"""Tests for the word-lookup pure logic (query building, HTML cleaning, field triage, ranking)."""

from __future__ import annotations

from omnia.plugins.word_lookup.logic import (
    KIND_AUDIO,
    KIND_IMAGE,
    KIND_TEXT,
    LookupCard,
    LookupField,
    build_query,
    card_state,
    escape_search_term,
    field_media,
    looks_like_identifier,
    rank_cards,
    strip_html,
    triage_fields,
    word_boundary_pattern,
    word_variants,
    words_boundary_pattern,
)


class TestBuildQuery:
    def test_quotes_the_term(self):
        assert build_query("plunge") == '"plunge"'

    def test_scopes_to_note_types(self):
        assert (
            build_query("plunge", ["AnkiVocabulary"])
            == '(note:"AnkiVocabulary" "plunge")'
        )

    def test_ors_multiple_note_types(self):
        # One clause per note type, so each can carry its own field scope.
        assert build_query("plunge", ["A", "B"]) == (
            '((note:"A" "plunge") OR (note:"B" "plunge"))'
        )

    def test_ignores_blank_note_type_entries(self):
        assert build_query("x", ["", "  ", "A"]) == '(note:"A" "x")'

    def test_restricts_to_the_configured_search_fields(self):
        query = build_query("level", ["V"], {"V": ["Word", "Meaning"]})
        assert query == (
            '(note:"V" ("Word:re:(?i)\\blevel\\b" OR "Meaning:re:(?i)\\blevel\\b"))'
        )

    def test_configured_fields_match_whole_words_not_substrings(self):
        # Measured on a real collection for "port": exact 5 (misses "port of call"),
        # substring 146 (Deport, Portion, Reporter, important…), word boundary 7 — correct.
        query = build_query("port", ["V"], {"V": ["Word"]})
        assert "*" not in query  # not a substring search
        assert r"\bport\b" in query

    def test_field_names_with_spaces_and_parens_stay_quoted(self):
        query = build_query("verb", ["V"], {"V": ["Word (part of speech)"]})
        assert '"Word (part of speech):re:' in query

    def test_unlisted_note_type_still_searches_all_its_fields(self):
        query = build_query("x", ["A", "B"], {"A": ["W"]})
        assert '(note:"A" ("W:re:' in query
        assert '(note:"B" "x")' in query

    def test_blank_field_entries_are_ignored(self):
        assert build_query("x", ["A"], {"A": ["", "  "]}) == '(note:"A" "x")'

    def test_blank_word_yields_no_query(self):
        assert build_query("   ") == ""
        assert build_query("", ["A"]) == ""

    def test_escapes_anki_search_syntax(self):
        # `*` and `_` are wildcards and `"` ends the phrase: unescaped they break/widen the search.
        assert escape_search_term('a"b*c_d') == 'a\\"b\\*c\\_d'
        assert build_query('a"b') == '"a\\"b"'


class TestStripHtml:
    def test_removes_tags_and_collapses_whitespace(self):
        assert strip_html("<div>hello   <b>world</b></div>") == "hello world"

    def test_removes_sound_and_image_refs(self):
        assert strip_html('[sound:a.mp3] hi <img src="b.jpg"> there') == "hi there"

    def test_unwraps_cloze_to_the_answer(self):
        assert strip_html("The {{c1::boy}} ran") == "The boy ran"
        assert strip_html("The {{c1::boy::person}} ran") == "The boy ran"

    def test_decodes_common_entities(self):
        assert strip_html("a&nbsp;b &amp; c &lt;d&gt;") == "a b & c <d>"

    def test_breaks_become_newlines_not_glued_words(self):
        # They used to become spaces; a list field then read as one blob (see
        # TestLineStructureIsKept). Either way the words must never be glued together.
        assert strip_html("one<br>two<br/>three") == "one\ntwo\nthree"
        assert strip_html("<p>one</p><p>two</p>") == "one\ntwo"

    def test_empty_input(self):
        assert strip_html("") == ""


class TestFieldMedia:
    def test_extracts_audio_and_images(self):
        audio, images = field_media('[sound:w.mp3] x <img src="p.jpg">')
        assert audio == ("w.mp3",) and images == ("p.jpg",)

    def test_handles_none_present(self):
        assert field_media("plain text") == ((), ())

    def test_multiple_refs(self):
        audio, _ = field_media("[sound:a.mp3][sound:b.mp3]")
        assert audio == ("a.mp3", "b.mp3")


class TestTriageFields:
    """The heart: make a 35-field note type readable without hardcoding field names."""

    def test_first_readable_field_becomes_the_title_and_is_not_repeated(self):
        title, fields = triage_fields([("Word", "plunge"), ("Definition", "to dive")])
        assert title == "plunge"
        assert [f.name for f in fields] == ["Definition"]

    def test_drops_fields_that_are_empty_after_cleaning(self):
        title, fields = triage_fields(
            [
                ("Word", "plunge"),
                ("Note ID", ""),
                ("Empty markup", "<div></div>"),
                ("Meaning", "lao xuống"),
            ]
        )
        assert title == "plunge"
        assert [f.name for f in fields] == ["Meaning"]  # the two empties vanished

    def test_keeps_note_type_field_order(self):
        _title, fields = triage_fields([("W", "w"), ("B", "b"), ("A", "a"), ("C", "c")])
        assert [f.name for f in fields] == [
            "B",
            "A",
            "C",
        ]  # authored order, not alphabetical

    def test_caps_the_number_of_fields(self):
        pairs = [("W", "w")] + [(f"F{i}", f"v{i}") for i in range(20)]
        _, fields = triage_fields(pairs, max_fields=3)
        assert len(fields) == 3

    def test_truncates_long_prose(self):
        _, fields = triage_fields([("W", "w"), ("Long", "x" * 500)], max_chars=50)
        assert len(fields[0].text) == 50

    def test_media_only_fields_become_badges(self):
        _, fields = triage_fields(
            [("W", "w"), ("Audio", "[sound:a.mp3]"), ("Image", '<img src="p.jpg">')]
        )
        by_name = {f.name: f for f in fields}
        assert by_name["Audio"].kind == KIND_AUDIO and by_name["Audio"].audio == (
            "a.mp3",
        )
        assert by_name["Image"].kind == KIND_IMAGE and by_name["Image"].images == (
            "p.jpg",
        )

    def test_text_with_media_is_still_text(self):
        _, fields = triage_fields([("W", "w"), ("Ex", "he dove in [sound:e.mp3]")])
        assert fields[0].kind == KIND_TEXT
        assert fields[0].text == "he dove in"
        assert fields[0].audio == ("e.mp3",)

    def test_hidden_fields_are_never_shown(self):
        title, fields = triage_fields(
            [("Note ID", "12345"), ("Word", "plunge"), ("Definition", "d")],
            hidden=("note id",),
        )
        assert title == "plunge"  # the hidden bookkeeping field did not steal the title
        assert [f.name for f in fields] == ["Definition"]

    def test_no_readable_field_yields_empty_title(self):
        title, fields = triage_fields([("Audio", "[sound:a.mp3]")])
        assert title == ""
        assert [f.name for f in fields] == ["Audio"]

    def test_empty_note(self):
        assert triage_fields([]) == ("", [])


class TestCardState:
    def test_maps_anki_types(self):
        assert card_state(0) == "new"
        assert card_state(1) == "learning"
        assert card_state(2) == "review"
        assert card_state(3) == "relearning"

    def test_unknown_defaults_to_review(self):
        assert card_state(99) == "review"


def _card(title: str, note_id: int = 1) -> LookupCard:
    return LookupCard(note_id=note_id, note_type="T", deck="D", title=title)


class TestRankCards:
    def test_exact_title_match_wins(self):
        ranked = rank_cards([_card("plunger"), _card("plunge")], "plunge")
        assert [c.title for c in ranked] == ["plunge", "plunger"]

    def test_prefix_beats_substring(self):
        ranked = rank_cards([_card("he plunged in"), _card("plunged")], "plunge")
        assert [c.title for c in ranked] == ["plunged", "he plunged in"]

    def test_is_case_insensitive(self):
        ranked = rank_cards([_card("zzz"), _card("Plunge")], "plunge")
        assert ranked[0].title == "Plunge"

    def test_ties_keep_original_order(self):
        cards = [_card("a", 1), _card("b", 2)]
        assert [c.note_id for c in rank_cards(cards, "nomatch")] == [1, 2]

    def test_empty_list(self):
        assert rank_cards([], "x") == []


class TestTitleWithRealWorldNoteTypes:
    """Regressions found by running the real collection through the pipeline."""

    def test_bookkeeping_first_field_does_not_become_the_title(self):
        # AnkiVocabulary's FIRST field is "Note ID" holding a UUID; naively titling on the first
        # readable field made every result read as a UUID.
        title, fields = triage_fields(
            [
                ("Note ID", "528b8776-d90d-11f0-9d54-838c99ec2e0d"),
                ("Word", "plunge"),
                ("Definition", "to dive"),
            ]
        )
        assert title == "plunge"
        # Note ID stays visible but sinks below real content (ordering, never hiding).
        assert [f.name for f in fields] == ["Definition", "Note ID"]

    def test_numeric_identifier_is_not_a_title_either(self):
        title, _ = triage_fields([("Note ID", "459"), ("Word", "swim")])
        assert title == "swim"

    def test_the_field_matching_the_word_wins_the_title(self):
        # Even mid-note: the card is titled by the field that IS the word being looked up.
        title, fields = triage_fields(
            [("Topic", "water sports"), ("Word", "plunge"), ("Def", "d")], word="plunge"
        )
        assert title == "plunge"
        assert "Topic" in [f.name for f in fields]

    def test_match_is_case_insensitive(self):
        title, _ = triage_fields([("A", "note"), ("Word", "Plunge")], word="plunge")
        assert title == "Plunge"

    def test_falls_back_when_no_field_matches(self):
        title, _ = triage_fields([("Word", "surf"), ("Def", "d")], word="plunge")
        assert title == "surf"

    def test_all_identifier_note_has_no_title(self):
        title, fields = triage_fields([("Note ID", "12345"), ("Other", "67890")])
        assert title == ""
        assert len(fields) == 2  # nothing is hidden — ordering only, never visibility


class TestRankingUsesEveryField:
    def test_exact_field_match_outranks_a_mere_mention(self):
        # "surf" merely mentions plunge in a synonyms field; the real "plunge" note must win
        # even though both titles are bookkeeping ids.
        surf = LookupCard(
            note_id=1,
            note_type="T",
            deck="D",
            title="459",
            fields=(LookupField(name="Synonyms", text="dive, plunge, drop"),),
        )
        plunge = LookupCard(
            note_id=2,
            note_type="T",
            deck="D",
            title="460",
            fields=(LookupField(name="Word", text="plunge"),),
        )
        ranked = rank_cards([surf, plunge], "plunge")
        assert [c.note_id for c in ranked] == [2, 1]

    def test_exact_title_still_beats_a_field_match(self):
        titled = LookupCard(note_id=1, note_type="T", deck="D", title="plunge")
        fielded = LookupCard(
            note_id=2,
            note_type="T",
            deck="D",
            title="x",
            fields=(LookupField(name="Word", text="plunge"),),
        )
        assert [c.note_id for c in rank_cards([fielded, titled], "plunge")] == [1, 2]


class TestLooksLikeIdentifier:
    def test_detects_identifier_shapes(self):
        assert looks_like_identifier("12345")
        assert looks_like_identifier("528b8776-d90d-11f0-9d54-838c99ec2e0d")
        assert looks_like_identifier("a3f9c2d10b4e6f88")

    def test_real_content_is_not_an_identifier(self):
        assert not looks_like_identifier("plunge")
        assert not looks_like_identifier("to dive in")
        assert not looks_like_identifier("")


class TestIdentifierFieldsSinkButStayVisible:
    def test_identifier_fields_move_to_the_end(self):
        _title, fields = triage_fields(
            [("Word", "plunge"), ("Note ID", "3113"), ("Definition", "to dive")]
        )
        # ordering changed, nothing removed
        assert [f.name for f in fields] == ["Definition", "Note ID"]

    def test_ordering_is_otherwise_preserved(self):
        _title, fields = triage_fields(
            [("W", "w"), ("B", "b"), ("A", "a"), ("Id", "42")]
        )
        assert [f.name for f in fields] == ["B", "A", "Id"]


class TestExplicitDisplayFields:
    """A per-note-type field list overrides the automatic pick, order included."""

    PAIRS = [
        ("Note ID", "1"),
        ("Word", "plunge"),
        ("Definition", "to dive"),
        ("Meaning (vi)", "lao xuống"),
        ("Example 1", "he plunged in"),
    ]

    def test_shows_only_the_listed_fields_in_the_listed_order(self):
        title, fields = triage_fields(
            self.PAIRS, word="plunge", only=("Meaning (vi)", "Definition")
        )
        assert title == "plunge"
        assert [f.name for f in fields] == ["Meaning (vi)", "Definition"]

    def test_is_case_insensitive_about_field_names(self):
        _title, fields = triage_fields(self.PAIRS, word="plunge", only=("definition",))
        assert [f.name for f in fields] == ["Definition"]

    def test_unknown_field_names_are_skipped(self):
        _title, fields = triage_fields(
            self.PAIRS, word="plunge", only=("Definition", "Nope")
        )
        assert [f.name for f in fields] == ["Definition"]

    def test_max_fields_does_not_truncate_an_explicit_list(self):
        # The list IS the user's choice; capping it would silently drop what they asked for.
        _title, fields = triage_fields(
            self.PAIRS,
            word="plunge",
            only=("Definition", "Meaning (vi)", "Example 1"),
            max_fields=1,
        )
        assert len(fields) == 3

    def test_empty_list_falls_back_to_the_automatic_pick(self):
        _title, auto = triage_fields(self.PAIRS, word="plunge")
        _title2, explicit_empty = triage_fields(self.PAIRS, word="plunge", only=())
        assert [f.name for f in auto] == [f.name for f in explicit_empty]


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

    def test_is_capped(self):
        assert len(word_variants("studies")) <= 6


class TestVariantsInTheQuery:
    def test_forms_are_one_alternation(self):
        query = build_query("loved", ["V"], {"V": ["Word"]})
        assert "(?:" in query and "love" in query
        assert query.count("Word:re:") == 1  # one clause, not one per form

    def test_can_be_switched_off(self):
        query = build_query("loved", ["V"], {"V": ["Word"]}, match_word_forms=False)
        assert "(?:" not in query
        assert r"\bloved\b" in query

    def test_boundary_is_dropped_only_on_the_side_that_cannot_carry_it(self):
        # "c++" starts with a word char (\b works) but ends with '+' (a trailing \b would make
        # the pattern match nothing), so only the RIGHT boundary is dropped.
        assert words_boundary_pattern(["c++"]) == r"(?i)\bc\+\+"
        assert words_boundary_pattern(["++c"]) == r"(?i)\+\+c\b"
        assert words_boundary_pattern(["go", "goes"]) == r"(?i)\b(?:go|goes)\b"


class TestLineStructureIsKept:
    """A field that lists one entry per <br> must not collapse into one blob."""

    RAW = (
        "crawl along + N: bò dọc theo  <br>crawl out of + Sth: thoát ra khỏi  "
        "<br>crawl up + N: bò lên  "
    )

    def test_br_becomes_a_newline(self):
        assert strip_html(self.RAW).split("\n") == [
            "crawl along + N: bò dọc theo",
            "crawl out of + Sth: thoát ra khỏi",
            "crawl up + N: bò lên",
        ]

    def test_closing_block_tags_also_break_the_line(self):
        assert strip_html("<div>one</div><div>two</div>") == "one\ntwo"
        assert strip_html("<li>a</li><li>b</li>") == "a\nb"

    def test_a_plain_sentence_stays_on_one_line(self):
        assert strip_html("<div>hello   <b>world</b></div>") == "hello world"

    def test_blank_lines_are_collapsed(self):
        assert strip_html("a<br><br><br>b") == "a\nb"

    def test_each_line_is_trimmed(self):
        assert strip_html("  a  <br>   b   ") == "a\nb"
