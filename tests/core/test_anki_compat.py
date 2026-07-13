"""Tests for pure helpers in ``omnia.core.anki_compat`` (Anki stubbed by conftest)."""

from __future__ import annotations

from omnia.core.anki_compat import (
    _guard,
    escape_search_term,
    subscribe_hook,
    unsubscribe_hook,
)


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


class TestHookGuard:
    """The single logging guard behind subscribe/unsubscribe (findings: ghost + filter guard)."""

    def test_resubscribe_after_failed_teardown_leaves_no_ghost(self, gui_hooks):
        # A double-subscribe (a failed teardown left the first wrapper, then a reload re-enabled)
        # must fully clear on the next disable — the earlier wrapper must not linger and keep firing.
        fired: list[int] = []

        def cb() -> None:
            fired.append(1)

        subscribe_hook("reviewer_did_show_question", cb)  # first (never unsubscribed)
        subscribe_hook("reviewer_did_show_question", cb)  # after reload
        unsubscribe_hook("reviewer_did_show_question", cb)  # must remove BOTH wrappers

        gui_hooks.reviewer_did_show_question.fire()
        assert fired == []
        assert gui_hooks.reviewer_did_show_question.count() == 0

    def test_guarded_filter_hook_returns_passthrough_on_error(self, gui_hooks):
        # Filter hooks are now guarded too: an exception must not crash the chain; the threaded
        # value passes straight through (grading/pycmd keep working).
        def boom(value, *_a):
            raise RuntimeError("feature bug")

        subscribe_hook("reviewer_will_answer_card", boom)
        result = gui_hooks.reviewer_will_answer_card.fire("PASSTHROUGH")
        assert result == "PASSTHROUGH"

    def test_guard_reraises_passthrough_arg_on_exception(self):
        def boom(x):
            raise RuntimeError("nope")

        sentinel = object()
        assert _guard("reviewer_will_answer_card", boom)(sentinel) is sentinel

    def test_guard_returns_none_when_no_args_on_exception(self):
        def boom():
            raise RuntimeError("nope")

        assert _guard("some_notify_hook", boom)() is None

    def test_guard_passes_through_return_value_on_success(self):
        assert _guard("some_hook", lambda a: a + 1)(41) == 42
