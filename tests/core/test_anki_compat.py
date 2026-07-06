"""Tests for pure helpers in ``omnia.core.anki_compat`` (Anki stubbed by conftest)."""

from __future__ import annotations

from omnia.core.anki_compat import escape_search_term


class TestEscapeSearchTerm:
    """L15: names with ``"`` / ``\\`` must not break an interpolated quoted search term."""

    def test_plain_name_unchanged(self):
        assert escape_search_term("Basic") == "Basic"

    def test_escapes_double_quote(self):
        assert escape_search_term('Basic "Q"') == 'Basic \\"Q\\"'

    def test_escapes_backslash(self):
        # A lone backslash is doubled (so it stays literal, not an escape of the next char).
        assert escape_search_term("a\\b") == "a\\\\b"

    def test_escapes_backslash_before_quote(self):
        # Backslash is escaped FIRST, so the quote's own escape backslash isn't re-doubled.
        assert escape_search_term('a\\"b') == 'a\\\\\\"b'
