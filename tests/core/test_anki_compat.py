"""Tests for pure helpers in ``omnia.core.anki_compat`` (Anki stubbed by conftest)."""

from __future__ import annotations

from omnia.core.anki_compat import (
    _guard,
    escape_search_term,
    media_still_referenced,
    progress_label,
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


class TestProgressLabel:
    """A background op retitling the progress dialog must not be able to crash Anki.

    ``progress_label`` marshals onto the Qt main thread, so its closure runs LATER, inside
    ``taskman._on_closures_pending`` — which has no try/except of its own. A guard on the
    calling side cannot protect anything that happens there.
    """

    def _mw_with_deferred_taskman(self, monkeypatch):
        """Install a fake ``mw`` whose taskman only STORES the closure, as the real one queues."""
        import aqt

        class _Progress:
            def __init__(self):
                self.labels = []

            def update(self, label=None, **_kwargs):
                self.labels.append(label)

        class _Taskman:
            def __init__(self):
                self.pending = []

            def run_on_main(self, callback):
                self.pending.append(callback)

        class _MW:
            def __init__(self):
                self.progress = _Progress()
                self.taskman = _Taskman()

        window = _MW()
        monkeypatch.setattr(aqt, "mw", window, raising=False)
        return window

    def test_the_label_reaches_the_dialog_on_the_main_thread(self, monkeypatch):
        window = self._mw_with_deferred_taskman(monkeypatch)

        progress_label("voice.onnx: 4.2/63.2 MB (6%)")

        assert window.progress.labels == []  # nothing touched Qt from the worker thread
        window.taskman.pending[0]()  # …until the main thread drains the queue
        assert window.progress.labels == ["voice.onnx: 4.2/63.2 MB (6%)"]

    def test_a_label_queued_before_shutdown_is_dropped_not_raised(self, monkeypatch):
        """Quitting Anki mid-download clears ``aqt.mw`` between the queue and the drain.

        The closure would then evaluate ``None.progress``; unguarded, that surfaces as Anki's
        error dialog with a traceback, for a background op the user never asked about.
        """
        import aqt

        window = self._mw_with_deferred_taskman(monkeypatch)
        progress_label("voice.onnx: 61.0/63.2 MB (96%)")

        monkeypatch.setattr(aqt, "mw", None, raising=False)

        window.taskman.pending[0]()  # must not raise


class TestTheToolsMenuSeam:
    """What the main window still holds after a plugin has been torn down.

    ``QWidget.removeAction`` detaches an action from the MENU and leaves it parented to the
    window. Every action here is created with ``mw`` as its parent, so without disposal a
    plugin toggled ten times leaves seventy behind — and anything that asks the window what it
    contains then sees a plugin's own retired actions. That is not hypothetical: the clash
    check below reported them as the thing holding the plugin's keys, and told the user to
    uninstall the add-on it is part of.
    """

    @staticmethod
    def _window(monkeypatch):
        import aqt
        from aqt.qt import FakeMainWindow

        window = FakeMainWindow()
        monkeypatch.setattr(aqt, "mw", window, raising=False)
        return window

    def test_a_removed_action_is_no_longer_on_the_window(self, monkeypatch):
        from aqt.qt import QAction

        from omnia.core.anki_compat import (
            add_tools_menu_action,
            remove_tools_menu_action,
        )

        window = self._window(monkeypatch)
        action = add_tools_menu_action("Omnia · Test", lambda _c: None, shortcut="]")
        assert window.findChildren(QAction) == [action]

        remove_tools_menu_action(action)

        assert window.findChildren(QAction) == []

    def test_teardown_and_re_enable_does_not_accumulate(self, monkeypatch):
        from aqt.qt import QAction

        from omnia.core.anki_compat import (
            add_tools_menu_action,
            remove_tools_menu_action,
        )

        window = self._window(monkeypatch)
        for _ in range(5):
            action = add_tools_menu_action(
                "Omnia · Test", lambda _c: None, shortcut="]"
            )
            remove_tools_menu_action(action)

        assert window.findChildren(QAction) == []

    def test_removing_nothing_is_harmless(self, monkeypatch):
        from omnia.core.anki_compat import remove_tools_menu_action

        self._window(monkeypatch)
        remove_tools_menu_action(None)  # must not raise


class TestShortcutAlreadyTaken:
    """Whether a key sequence is spoken for, asked before a plugin binds its own."""

    @staticmethod
    def _window(monkeypatch):
        import aqt
        from aqt.qt import FakeMainWindow

        window = FakeMainWindow()
        monkeypatch.setattr(aqt, "mw", window, raising=False)
        return window

    def test_a_free_sequence_is_reported_free(self, monkeypatch):
        from omnia.core.anki_compat import shortcut_already_taken

        self._window(monkeypatch)
        assert shortcut_already_taken("]") == []

    def test_it_names_the_action_holding_the_key(self, monkeypatch):
        from omnia.core.anki_compat import add_tools_menu_action, shortcut_already_taken

        self._window(monkeypatch)
        add_tools_menu_action("Speed Up Audio", lambda _c: None, shortcut="]")

        assert shortcut_already_taken("]") == ["Speed Up Audio"]

    def test_a_different_key_is_not_a_clash(self, monkeypatch):
        from omnia.core.anki_compat import add_tools_menu_action, shortcut_already_taken

        self._window(monkeypatch)
        add_tools_menu_action("Speed Up Audio", lambda _c: None, shortcut="]")

        assert shortcut_already_taken("[") == []

    def test_an_ampersand_accelerator_is_not_part_of_the_name(self, monkeypatch):
        # Qt menu text carries "&" to mark the Alt accelerator; it is not part of what the
        # user sees, and printing it back at them reads as a typo.
        from omnia.core.anki_compat import add_tools_menu_action, shortcut_already_taken

        self._window(monkeypatch)
        add_tools_menu_action("&Speed Up", lambda _c: None, shortcut="]")

        assert shortcut_already_taken("]") == ["Speed Up"]

    def test_an_empty_sequence_matches_nothing(self, monkeypatch):
        # Most actions have no shortcut at all; treating "" as a match would name every one.
        from omnia.core.anki_compat import add_tools_menu_action, shortcut_already_taken

        self._window(monkeypatch)
        add_tools_menu_action("No Shortcut", lambda _c: None)

        assert shortcut_already_taken("") == []

    def test_a_retired_action_is_not_reported(self, monkeypatch):
        # The defect this whole class exists for: reconfiguring a plugin tears its actions
        # down and re-enables it, and the check ran against the corpses.
        from omnia.core.anki_compat import (
            add_tools_menu_action,
            remove_tools_menu_action,
            shortcut_already_taken,
        )

        self._window(monkeypatch)
        action = add_tools_menu_action(
            "Omnia · Audio: speed up", lambda _c: None, shortcut="]"
        )
        remove_tools_menu_action(action)

        assert shortcut_already_taken("]") == []

    def test_no_main_window_is_answered_not_raised(self, monkeypatch):
        # A diagnostic that breaks the feature it is diagnosing is worse than no diagnostic.
        import aqt

        from omnia.core.anki_compat import shortcut_already_taken

        monkeypatch.setattr(aqt, "mw", None, raising=False)
        assert shortcut_already_taken("]") == []


class TestBorrowingTheMainThreadForTheCollection:
    """For an op that runs OFF Anki's collection thread but still has one call to make.

    Anki serialises collection access through a single thread, and an op that holds it for
    minutes makes everything else queue behind it — which is what put a modal "Processing…"
    over the app for the length of a Smart Notes batch. Releasing that thread is only safe if
    the collection access inside the op goes somewhere Anki considers entitled to it.
    """

    def _window(self, in_main: bool, run):
        class _Taskman:
            def __init__(self):
                self.posted = []

            def run_on_main(self, closure):
                self.posted.append(closure)
                run(closure)

        class _Window:
            def __init__(self):
                self.taskman = _Taskman()

            def inMainThread(self):  # Anki's own method name
                return in_main

        return _Window()

    def test_a_worker_hands_the_work_over_and_gets_the_answer_back(self, monkeypatch):
        import threading

        from omnia.core import anki_compat

        ran_on: list = []

        def elsewhere(closure):
            # A real second thread, like Anki's. Running it inline would make "on the main
            # thread" and "on this one" indistinguishable and the test unable to fail.
            thread = threading.Thread(target=closure, name="pretend-main")
            thread.start()
            thread.join(5)

        window = self._window(in_main=False, run=elsewhere)
        monkeypatch.setattr(anki_compat, "main_window", lambda: window)

        answer = anki_compat.call_on_main_and_wait(
            lambda: ran_on.append(threading.current_thread().name) or "written"
        )

        assert answer == "written"
        assert ran_on == ["pretend-main"]

    def test_on_the_main_thread_it_just_runs(self, monkeypatch):
        # Posting and then waiting would deadlock: the thread that has to run the closure is
        # the one doing the waiting.
        from omnia.core import anki_compat

        window = self._window(in_main=True, run=lambda closure: closure())
        monkeypatch.setattr(anki_compat, "main_window", lambda: window)

        assert anki_compat.call_on_main_and_wait(lambda: "done") == "done"
        assert window.taskman.posted == [], "it queued work behind itself"

    def test_an_exception_comes_back_to_the_caller(self, monkeypatch):
        import pytest

        from omnia.core import anki_compat

        def boom():
            raise ValueError("no disk")

        window = self._window(in_main=False, run=lambda closure: closure())
        monkeypatch.setattr(anki_compat, "main_window", lambda: window)

        with pytest.raises(ValueError, match="no disk"):
            anki_compat.call_on_main_and_wait(boom)

    def test_a_main_thread_that_never_answers_gives_up(self, monkeypatch):
        # Not unbounded: a worker blocked forever on a main thread that will never answer keeps
        # the whole batch alive with nothing to show for it.
        import pytest

        from omnia.core import anki_compat

        monkeypatch.setattr(anki_compat, "_MAIN_THREAD_WAIT_SECONDS", 0.2)
        window = self._window(in_main=False, run=lambda closure: None)
        monkeypatch.setattr(anki_compat, "main_window", lambda: window)

        with pytest.raises(TimeoutError):
            anki_compat.call_on_main_and_wait(lambda: "never")


class TestWritingMediaFromAWorker:
    def test_it_goes_through_the_main_thread(self, monkeypatch):
        # The one piece of collection access inside the generation op. If it stopped hopping,
        # the op would be touching the collection from a thread Anki does not expect.
        from omnia.core import anki_compat

        hopped: list = []
        monkeypatch.setattr(
            anki_compat,
            "call_on_main_and_wait",
            lambda work: hopped.append(True) or work(),
        )

        class _Media:
            def write_data(self, filename, data):
                return "stored-" + filename

        class _Window:
            col = type("C", (), {"media": _Media()})()

        monkeypatch.setattr(anki_compat, "main_window", lambda: _Window())

        assert anki_compat.add_media_file("a.mp3", b"x") == "stored-a.mp3"
        assert hopped == [True], "the media write never left the worker thread"

    def test_an_explicit_collection_is_used_as_given(self, monkeypatch):
        # The caller named the collection and, by doing so, said it is already on a thread
        # entitled to touch it — hopping again would be a pointless round trip.
        from omnia.core import anki_compat

        monkeypatch.setattr(
            anki_compat,
            "call_on_main_and_wait",
            lambda work: (_ for _ in ()).throw(AssertionError("hopped needlessly")),
        )

        class _Media:
            def write_data(self, filename, data):
                return "direct-" + filename

        col = type("C", (), {"media": _Media()})()

        assert anki_compat.add_media_file("b.mp3", b"y", col) == "direct-b.mp3"


class TestAskingWhoElseStillPlaysAFile:
    """Before a regenerated field's old audio is trashed, who else points at it.

    A filename is not owned by the note that created it: *Notes → Create Copy* leaves two notes
    carrying the same ``[sound:omnia-….mp3]``. Regenerating the original stops IT referencing
    the file, and trashing on that basis alone silences the copy.
    """

    class _Col:
        def __init__(self, hits=None):
            self.queries: list = []
            self._hits = hits or {}

        def find_notes(self, query):
            self.queries.append(query)
            for needle, result in self._hits.items():
                if needle in query:
                    return result
            return []

    def test_nothing_to_check_asks_nothing(self):
        col = self._Col()

        assert media_still_referenced([], col=col) == set()
        assert col.queries == []

    def test_the_common_case_costs_one_query(self):
        """The search scans every note, so one query per file scans the collection per file.

        A slice of 25 notes would scan it 25 times, on every slice, for a check that almost
        always comes back empty.
        """
        col = self._Col()

        assert media_still_referenced(["a.mp3", "b.mp3"], col=col) == set()
        assert len(col.queries) == 1
        assert "a.mp3" in col.queries[0] and "b.mp3" in col.queries[0]

    def test_it_excludes_no_note_at_all(self):
        """Deliberately: the note just written is frequently the referent that matters.

        Anki's ``add_data_to_folder_uniquely`` hashes before it renames, so byte-identical
        media comes back under the SAME filename. A regeneration producing identical audio
        therefore leaves the note pointing at the very file its old value pointed at. An
        "ignore the notes I just wrote" argument would hide that note and trash a live file —
        silencing exactly the notes a re-run had just regenerated.
        """
        col = self._Col()

        media_still_referenced(["a.mp3"], col=col)

        assert "-nid" not in col.queries[0]

    def test_a_hit_is_narrowed_to_the_file_that_caused_it(self):
        # Only then is it worth paying for a query per file.
        col = self._Col(hits={"b.mp3": [42]})

        still = media_still_referenced(["a.mp3", "b.mp3"], col=col)

        assert still == {"b.mp3"}

    def test_a_search_that_will_not_run_trashes_nothing(self):
        """The safe default is keeping the file: one costs disk, the other costs audio."""

        class _Broken:
            def find_notes(self, query):
                raise RuntimeError("invalid search")

        assert media_still_referenced(["a.mp3"], col=_Broken()) == {"a.mp3"}

    def test_a_field_name_with_brackets_survives_the_query(self):
        """Media is named ``omnia-<nid>-<field>.<ext>`` and real field names look like
        ``Example 1 (audio)``. Anki rejects an UNRECOGNISED escape outright, so escaping the
        brackets "to be safe" would turn every such check into the error path above."""
        col = self._Col()

        media_still_referenced(["omnia-1-Example 1 (audio).mp3"], col=col)

        assert "(audio)" in col.queries[0]

    def test_wildcards_in_a_name_cannot_match_other_files(self):
        col = self._Col()

        media_still_referenced(["a*b_c.mp3"], col=col)

        assert "a\\*b\\_c.mp3" in col.queries[0]
