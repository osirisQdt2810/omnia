"""Tests for the shared markup cleaner (what the panel shows and what a voice speaks)."""

from __future__ import annotations

from omnia.core.lang.text import as_field_html, strip_markup


class TestTagsAndEntities:
    def test_removes_inline_tags(self):
        # The bug that motivated this: a voice read "strong" aloud for <strong>.
        assert strip_markup("<strong>hello</strong> world") == "hello world"

    def test_decodes_entities(self):
        assert strip_markup("a&nbsp;&amp;&nbsp;b") == "a & b"
        assert strip_markup("x&mdash;y") == "x—y"

    def test_collapses_runs_of_spaces(self):
        assert strip_markup("a   b\t\tc") == "a b c"


class TestAnkiSyntaxes:
    def test_drops_sound_references(self):
        # A [sound:…] is a FILENAME; speaking it would read the file name out loud.
        assert strip_markup("plunge [sound:4000B6_plunge.mp3]") == "plunge"

    def test_drops_images(self):
        assert strip_markup('<img src="p.jpg"> a picture') == "a picture"

    def test_drops_anki_av_directives(self):
        assert strip_markup("hi [anki:tts lang=en_US]word[/anki:tts]") == "hi word"

    def test_unwraps_cloze_to_the_answer(self):
        assert strip_markup("The {{c1::boy}} ran") == "The boy ran"

    def test_cloze_hint_is_dropped(self):
        assert strip_markup("The {{c1::boy::person}} ran") == "The boy ran"


class TestLineBreaks:
    def test_block_breaks_become_newlines(self):
        assert strip_markup("one<br>two<br/>three") == "one\ntwo\nthree"
        assert strip_markup("<div>a</div><div>b</div>") == "a\nb"

    def test_repeated_blank_lines_fold(self):
        assert strip_markup("a<br><br><br>b") == "a\nb"

    def test_can_flatten_for_a_consumer_that_cannot_show_them(self):
        assert strip_markup("one<br>two", keep_line_breaks=False) == "one two"


class TestEmptyResults:
    def test_markup_with_no_words_becomes_empty(self):
        # Matters for TTS: an empty result must be detectable so no request is made at all.
        assert strip_markup("<div><br></div>") == ""
        assert strip_markup('<img src="a.jpg">') == ""
        assert strip_markup("[sound:a.mp3]") == ""

    def test_blank_input(self):
        assert strip_markup("") == ""


class TestAsFieldHtml:
    """The other direction: plain text going back INTO a field, which stores HTML."""

    def test_escapes_markup_characters(self):
        assert as_field_html("5 < 6 & rising") == "5 &lt; 6 &amp; rising"

    def test_line_breaks_become_br_tags(self):
        # A bare newline in stored HTML renders as a single space — the lines would merge.
        assert as_field_html("one\ntwo") == "one<br>two"

    def test_a_quote_is_left_readable(self):
        # Field text is never interpolated into an attribute, so quoting it would only make
        # the stored value uglier (&quot;) for no gain.
        assert as_field_html('he said "hi"') == 'he said "hi"'

    def test_round_trips_through_strip_markup(self):
        # The pair is a fixed point: what a task re-derives equals what it wrote last run.
        text = "5 < 6 & rising\nreally"
        assert strip_markup(as_field_html(text)) == text

    def test_blank_input(self):
        assert as_field_html("") == ""


class TestEveryEntityIsDecoded:
    """An undecoded entity reaches a TTS provider verbatim and gets read out as characters.

    The old hand-kept table covered six entities, so "caf&eacute;" was spoken as its literal
    characters. Invisible formatting characters are removed for the same reason one layer down:
    they split a word for every consumer that looks at the text.
    """

    def test_a_named_entity_outside_the_old_table(self):
        assert strip_markup("caf&eacute; au lait") == "café au lait"

    def test_nbsp_becomes_a_plain_space_not_u00a0(self):
        # html.unescape would give U+00A0, which a voice and a \b regex both handle worse.
        assert strip_markup("a&nbsp;b") == "a b"

    def test_an_escaped_tag_stays_visible_text(self):
        assert strip_markup("1 &lt; 2") == "1 < 2"

    def test_invisible_formatting_characters_are_removed(self):
        # A soft hyphen inside a word makes a word-boundary match miss it silently.
        assert strip_markup("sur&shy;vived") == "survived"
        assert strip_markup("zero​width") == "zerowidth"
