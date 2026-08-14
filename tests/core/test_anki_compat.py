"""Tests for pure helpers in ``omnia.core.anki_compat`` (Anki stubbed by conftest)."""

from __future__ import annotations

from omnia.core.anki_compat import (
    _guard,
    escape_search_term,
    random_note_of_type,
    subscribe_hook,
    unsubscribe_hook,
)


class _FakeCollection:
    """A collection reduced to the two calls ``random_note_of_type`` makes."""

    def __init__(self, note_ids: list[int]) -> None:
        self._note_ids = list(note_ids)
        self.queries: list[str] = []

    def find_notes(self, query: str) -> list[int]:
        self.queries.append(query)
        return list(self._note_ids)

    def get_note(self, note_id: int) -> str:
        return f"note-{note_id}"


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


class TestRandomNoteOfType:
    """The preview must sample the collection, not keep showing the same card.

    ``note_ids[0]`` made every preview of a note type return one identical note, so a prompt
    that happened to suit it looked correct and a rule that only broke on other notes looked
    fine — the exact failure a preview exists to prevent.
    """

    def test_it_picks_from_the_whole_result_set(self, monkeypatch):
        seen = {}

        def fake_choice(population):
            seen["population"] = list(population)
            return population[-1]

        monkeypatch.setattr("omnia.core.anki_compat.random.choice", fake_choice)
        col = _FakeCollection([11, 22, 33])

        note = random_note_of_type("Basic", col=col)

        # EVERY id is offered — not a slice, and not the first one taken directly.
        assert seen["population"] == [11, 22, 33]
        assert note == "note-33"

    def test_no_notes_gives_none_rather_than_an_empty_choice(self, monkeypatch):
        # `random.choice([])` raises IndexError; a note type with no notes is ordinary.
        def explode(_population):
            raise AssertionError("random.choice must not be called for an empty result")

        monkeypatch.setattr("omnia.core.anki_compat.random.choice", explode)

        assert random_note_of_type("Basic", col=_FakeCollection([])) is None

    def test_the_note_type_is_escaped_into_the_query(self):
        col = _FakeCollection([1])

        random_note_of_type('Basic "Q"', col=col)

        assert col.queries == ['note:"Basic \\"Q\\""']
