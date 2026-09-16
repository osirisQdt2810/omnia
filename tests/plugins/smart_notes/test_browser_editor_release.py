"""Letting go of the Browser editor's note before a batch rewrites notes underneath it.

Found by probing a real run: a batch started from the Browser made Anki open a modal
"Processing…" window twelve times in forty seconds, each one blocking every other window. Every
step of the loop is Anki's own and reasonable on its own:

1. the batch writes notes, so `operation_did_execute` fires with no handler;
2. `Browser.on_operation_did_execute` sees `handler is not self.editor` and RELOADS the editor's
   note;
3. loading it runs the editor's JS, which posts a `blur`/`key` bridge command;
4. `Editor.onBridgeCmd` answers with `_save_current_note()` — a `CollectionOp`, and every
   `CollectionOp` goes through `taskman.with_progress(op, on_done)` with no label, which
   `ProgressManager.start` renders as "Processing…", ApplicationModal.

With no note in the editor, step 2 has nothing to reload and the chain never starts.
"""

from __future__ import annotations

from omnia.plugins.smart_notes import _release_browser_editor


class _Editor:
    """Anki's editor, reduced to the two calls that matter here."""

    def __init__(self, note: object | None = "a note") -> None:
        self.note = note
        self.saved = False
        self.set_to: list = []
        self._pending = None

    def call_after_note_saved(
        self, callback, keepFocus=False
    ):  # Anki's own parameter name
        # Asynchronous in Anki: it round-trips through the editor's webview. Held rather than
        # called, so a test can prove the batch waits for it.
        self.saved = True
        self._pending = callback

    def flush(self):
        """Run what Anki would run once the webview answered."""
        callback, self._pending = self._pending, None
        if callback is not None:
            callback()

    def set_note(self, note, hide=True, focusTo=None):  # Anki's own parameter name
        self.note = note
        self.set_to.append((note, hide))


class _Browser:
    def __init__(self, editor):
        self.editor = editor


class TestReleasingTheEditor:
    def test_the_note_is_saved_before_it_is_dropped(self):
        # Dropping it unsaved would discard whatever the user had typed and not committed —
        # which is why Anki's own `begin_reset` says the caller must have saved first.
        editor = _Editor()
        started: list = []

        _release_browser_editor(_Browser(editor), lambda: started.append(True))

        assert editor.saved is True
        assert started == [], "the batch started before the editor had saved"

    def test_the_editor_lets_go_of_the_note(self):
        editor = _Editor()
        _release_browser_editor(_Browser(editor), lambda: None)

        editor.flush()

        assert editor.note is None
        assert editor.set_to == [
            (None, False)
        ], "the splitter was hidden as a side effect"

    def test_the_batch_runs_after_the_save_lands(self):
        editor = _Editor()
        started: list = []
        _release_browser_editor(_Browser(editor), lambda: started.append(True))

        editor.flush()

        assert started == [True]

    def test_an_editor_holding_nothing_does_not_wait(self):
        # Nothing to save and nothing to reload, so there is no reason to make the user wait a
        # webview round trip before their batch starts.
        editor = _Editor(note=None)
        started: list = []

        _release_browser_editor(_Browser(editor), lambda: started.append(True))

        assert started == [True]
        assert editor.saved is False


class TestItNeverCostsTheBatch:
    """The worst case of getting this wrong is the old behaviour, not a lost generation."""

    def test_no_browser_still_runs_the_batch(self):
        started: list = []

        _release_browser_editor(None, lambda: started.append(True))

        assert started == [True]

    def test_a_browser_with_no_editor_still_runs_the_batch(self):
        started: list = []

        _release_browser_editor(_Browser(None), lambda: started.append(True))

        assert started == [True]

    def test_an_editor_that_cannot_save_still_runs_the_batch(self):
        class _Refuses(_Editor):
            def call_after_note_saved(self, callback, keepFocus=False):
                raise RuntimeError("no webview")

        started: list = []

        _release_browser_editor(_Browser(_Refuses()), lambda: started.append(True))

        assert started == [True]

    def test_an_editor_that_cannot_let_go_still_runs_the_batch(self):
        class _Stuck(_Editor):
            def set_note(self, note, hide=True, focusTo=None):
                raise RuntimeError("deleted widget")

        editor = _Stuck()
        started: list = []
        _release_browser_editor(_Browser(editor), lambda: started.append(True))

        editor.flush()

        assert started == [True]
