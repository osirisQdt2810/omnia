"""Tests for the shared markup cleaner (what the panel shows and what a voice speaks)."""

from __future__ import annotations

from omnia.core.text import as_field_html, strip_markup


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
