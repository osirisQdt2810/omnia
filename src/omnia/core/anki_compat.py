"""Thin shims over Anki APIs that differ across versions.

All Anki imports are **lazy** (inside functions) so this module imports cleanly headless;
the functions only do real work inside a running Anki. Centralising the version quirks
here keeps features free of ``hasattr`` checks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional, TypeVar

T = TypeVar("T")


def main_window() -> Any:
    """Return Anki's main window ``mw`` (raises if Anki isn't loaded)."""
    from aqt import mw

    return mw


def gui_hooks() -> Any:
    """Return the ``aqt.gui_hooks`` module."""
    from aqt import gui_hooks

    return gui_hooks


def next_interval_seconds(
    card: Any, ease: int, col: Optional[Any] = None
) -> Optional[int]:
    """Return the next interval (seconds) if ``card`` were answered at ``ease``.

    Handles the scheduler method renamed across Anki versions (``nextIvl`` →
    ``next_ivl``). Returns None if neither is available.

    Args:
        card: The card to predict for.
        ease: Ease button (1=Again .. 4=Easy).
        col: The collection; defaults to ``mw.col``.
    """
    if col is None:
        col = main_window().col
    sched = col.sched
    for attr in ("nextIvl", "next_ivl"):
        method = getattr(sched, attr, None)
        if callable(method):
            return int(method(card, ease))
    return None


def card_last_review_ms(card: Any, col: Optional[Any] = None) -> Optional[int]:
    """Return the epoch-ms timestamp of the card's most recent review, or None.

    Reads the newest ``revlog`` row for the card; falls back to ``card.mod`` (seconds).
    """
    if col is None:
        col = main_window().col
    row = col.db.scalar("select max(id) from revlog where cid = ?", card.id)
    if row:
        return int(row)  # a revlog id IS the review epoch in milliseconds
    mod = getattr(card, "mod", None)
    return int(mod) * 1000 if mod else None


# --- threading / scheduling (keep network & heavy work off the Qt main thread) ---------
def run_in_background(
    op: Callable[[], T],
    *,
    on_success: Callable[[T], None],
    on_failure: Optional[Callable[[Exception], None]] = None,
    parent: Optional[Any] = None,
    label: Optional[str] = None,
) -> None:
    """Run ``op`` off the Qt main thread, then call ``on_success`` back on the main thread.

    Wraps ``aqt.operations.QueryOp`` so features never import ``aqt`` for async work. ``op``
    takes no arguments (do pure compute/network in it); apply results to the collection in
    ``on_success`` (which runs on the main thread).

    Args:
        op: The background callable returning a result.
        on_success: Main-thread callback receiving the result.
        on_failure: Optional main-thread callback receiving an exception.
        parent: Qt parent for the operation (defaults to ``mw``).
        label: Optional progress-dialog label.
    """
    from aqt.operations import QueryOp

    mw = main_window()
    query = QueryOp(parent=parent or mw, op=lambda _col: op(), success=on_success)
    if label:
        query = query.with_progress(label)
    if on_failure is not None and hasattr(query, "failure"):
        query = query.failure(on_failure)
    query.run_in_background()


def run_after(ms: int, callback: Callable[[], None]) -> Any:
    """Schedule ``callback`` on the Qt main thread after ``ms`` ms (one-shot). Returns the timer."""
    return main_window().progress.timer(ms, callback, False)


# --- reviewer controls (used by auto_flip) ---------------------------------------------
def reviewer_side() -> Optional[str]:
    """Return the reviewer's side ('question' | 'answer'), or None if not reviewing."""
    reviewer = getattr(main_window(), "reviewer", None)
    return getattr(reviewer, "state", None) if reviewer is not None else None


def effective_deck_id(card: Any) -> Optional[int]:
    """Return the deck whose options apply to ``card`` (``card.odid or card.did``).

    For cards pulled into a filtered/cram deck, ``did`` is the temporary filtered deck and
    ``odid`` is the card's *original* (home) deck — whose options the user actually
    configured. Anki resolves per-deck config the same way (see
    ``decks.config_dict_for_deck_id``), so per-deck overrides must key off the home deck.
    """
    odid = getattr(card, "odid", 0)
    return odid or getattr(card, "did", None)


def card_side_av_text(card: Any, side: str) -> str:
    """Return the rendered text of ``card``'s ``side`` ('question' | 'answer'), or ''.

    The reference auto-flip add-on scans the rendered question/answer HTML for its external
    "myview.mpv" clip-player command (which carries a ``--range=`` clip duration). We
    replicate that by returning the rendered side text; the pure
    :func:`~omnia.plugins.auto_flip.logic.parse_mpv_range_extra_seconds` parses it.
    """
    method = getattr(card, side, None)  # card.question() / card.answer()
    if not callable(method):
        return ""
    try:
        return str(method())
    except Exception:
        from omnia.core.logging import get_logger

        get_logger().exception("card_side_av_text: failed to render %s", side)
        return ""


def current_card() -> Any:
    """Return the card currently shown in the reviewer, or None."""
    reviewer = getattr(main_window(), "reviewer", None)
    return getattr(reviewer, "card", None) if reviewer is not None else None


def audio_still_playing() -> bool:
    """Return True if Anki's ``av_player`` still has queued/playing audio.

    Used to arm auto-flip only once the audio queue drains: the
    ``av_player_did_end_playing`` hook fires per clip, so a card with several sounds reports
    a non-empty queue until the last one ends.
    """
    from aqt.sound import av_player

    return bool(getattr(av_player, "_enqueued", ()))


def reviewer_show_answer() -> None:
    """Flip the current card to its answer side."""
    main_window().reviewer._showAnswer()


def reviewer_answer_card(ease: int) -> None:
    """Grade the current card at ``ease`` (routes through the ease pipeline)."""
    main_window().reviewer._answerCard(ease)


def reviewer_eval(js: str) -> None:
    """Evaluate ``js`` in the reviewer webview (no-op if not currently reviewing).

    For pushing dynamic JS into the card webview *after* it has rendered (e.g. an
    auto-flip countdown that ticks). Static per-card JS should go through the web
    injector instead; use this only for imperative updates between renders.
    """
    reviewer = getattr(main_window(), "reviewer", None)
    web = getattr(reviewer, "web", None) if reviewer is not None else None
    if web is not None:
        web.eval(js)


def main_web_eval(js: str) -> None:
    """Evaluate ``js`` in Anki's main webview (the deck list / overview / stats screen).

    Used by features that decorate the non-reviewer screens (e.g. a typed-accuracy
    stats card on the deck overview). No-op if the main webview isn't available.
    """
    web = getattr(main_window(), "web", None)
    if web is not None:
        web.eval(js)


# --- collection writes (call on the main thread / inside on_success) -------------------
def add_media_file(filename: str, data: bytes, col: Optional[Any] = None) -> str:
    """Write ``data`` as ``filename`` into the collection media folder; return the real name."""
    if col is None:
        col = main_window().col
    return str(col.media.write_data(filename, data))


def update_note(note: Any, col: Optional[Any] = None) -> None:
    """Persist edits to ``note`` (must run on the main thread)."""
    if col is None:
        col = main_window().col
    col.update_note(note)


# --- collection reads (note types, decks, notes) ---------------------------------------
def note_type_names(col: Optional[Any] = None) -> list[str]:
    """Return every note-type (model) name in the collection."""
    if col is None:
        col = main_window().col
    return [model["name"] for model in col.models.all()]


def note_type_field_names(note_type: str, col: Optional[Any] = None) -> list[str]:
    """Return the field names of ``note_type`` (empty if the note type is unknown)."""
    if col is None:
        col = main_window().col
    model = col.models.by_name(note_type)
    if model is None:
        return []
    return [field["name"] for field in model["flds"]]


def add_note_type_field(
    note_type: str, field_name: str, col: Optional[Any] = None
) -> list[str]:
    """Add ``field_name`` to ``note_type``'s schema and save it; return its field names.

    Used by the Smart Notes config UI's "Create field" action. A no-op (returns the existing
    names) when the note type is unknown or the field already exists. Must run on the main
    thread — it mutates the collection's note-type schema.
    """
    if col is None:
        col = main_window().col
    model = col.models.by_name(note_type)
    if model is None:
        return []
    existing = [field["name"] for field in model["flds"]]
    if field_name in existing:
        return existing
    field = col.models.new_field(field_name)
    col.models.add_field(model, field)
    col.models.update_dict(model)
    return [field["name"] for field in model["flds"]]


def deck_names(col: Optional[Any] = None) -> list[tuple[int, str]]:
    """Return ``(deck_id, deck_name)`` pairs for every deck (for the deck picker)."""
    if col is None:
        col = main_window().col
    return [(int(deck.id), deck.name) for deck in col.decks.all_names_and_ids()]


def find_note_ids(query: str, col: Optional[Any] = None) -> list[int]:
    """Return the note ids matching a search ``query`` (e.g. ``'note:"Basic"'``)."""
    if col is None:
        col = main_window().col
    return [int(nid) for nid in col.find_notes(query)]


def find_card_note_ids(query: str, col: Optional[Any] = None) -> list[int]:
    """Return the note ids of the cards matching a search ``query``."""
    if col is None:
        col = main_window().col
    return [int(col.get_card(cid).nid) for cid in col.find_cards(query)]


def get_note(nid: int, col: Optional[Any] = None) -> Any:
    """Return the note with id ``nid``."""
    if col is None:
        col = main_window().col
    return col.get_note(nid)


def random_note_of_type(
    note_type: str, deck_id: Optional[int] = None, col: Optional[Any] = None
) -> Any:
    """Return a note of ``note_type`` (optionally within ``deck_id``), or None if none exist.

    Used by the prompt dialog's "Test With Random Note" — it just needs any real note of the
    rule's note type to interpolate the prompt against.
    """
    if col is None:
        col = main_window().col
    query = f'note:"{note_type}"'
    if deck_id is not None:
        deck_name = col.decks.name(deck_id)
        if deck_name:
            query = f'{query} (deck:"{deck_name}" or deck:"{deck_name}::*")'
    note_ids = col.find_notes(query)
    return col.get_note(note_ids[0]) if note_ids else None


def play_audio(data: bytes, ext: str) -> None:
    """Play raw audio ``data`` through Anki's av player (writes a temp clip first).

    Used by the prompt/custom dialogs to preview a generated TTS clip without saving a rule.
    """
    import tempfile
    from pathlib import Path

    from aqt.sound import av_player

    tmp = Path(tempfile.gettempdir()) / f"omnia-preview.{ext or 'mp3'}"
    tmp.write_bytes(data)
    av_player.play_file(str(tmp))


def redraw_reviewer_current_card() -> None:
    """Re-render the reviewer's current card (best-effort; no-op if not reviewing).

    Used by review-time generation to refresh a card whose fields were just filled in the
    background. The private ``_redraw_current_card`` exists across the 25.09 line; falls back
    to re-showing the question.
    """
    reviewer = getattr(main_window(), "reviewer", None)
    if reviewer is None:
        return
    for attr in ("_redraw_current_card", "_showQuestion"):
        method = getattr(reviewer, attr, None)
        if callable(method):
            method()
            return


# --- progress dialog (cancellable counted batch) ---------------------------------------
def progress_start(label: str, maximum: int) -> None:
    """Open Anki's progress dialog with a cancel button (call on the main thread)."""
    main_window().progress.start(label=label, min=0, max=maximum, immediate=True)


def progress_update(label: str, value: int, maximum: int) -> None:
    """Update the progress dialog's label/value (call on the main thread)."""
    main_window().progress.update(label=label, value=value, max=maximum)


def progress_finish() -> None:
    """Close the progress dialog (call on the main thread)."""
    main_window().progress.finish()


def progress_was_cancelled() -> bool:
    """Return whether the user clicked Cancel in the progress dialog."""
    return bool(main_window().progress.want_cancel())


def run_on_main(callback: Callable[[], None]) -> None:
    """Schedule ``callback`` to run on the Qt main thread (from a background thread)."""
    main_window().taskman.run_on_main(callback)


# --- hook subscription (so features stay free of direct gui_hooks access) --------------
# Filter hooks must RETURN a value (the threaded result); we never wrap those — their handlers
# are trivial and return-critical. Every other (notify) hook callback is wrapped in a logging
# guard so a single feature's bug logs to omnia.log instead of crashing Anki's UI on a click.
_FILTER_HOOKS = frozenset(
    {"reviewer_will_answer_card", "webview_did_receive_js_message"}
)
# (hook_name, original_callback) -> guarded wrapper actually registered, for clean removal.
_GUARDED: dict[tuple[str, Any], Callable[..., Any]] = {}


def _guard(hook_name: str, callback: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # A notify-hook bug must not break Anki's UI — log it and continue.
        try:
            return callback(*args, **kwargs)
        except Exception:
            from omnia.core.logging import get_logger

            get_logger().exception("hook %s callback failed", hook_name)
            return None

    return wrapper


def subscribe_hook(hook_name: str, callback: Callable[..., Any]) -> None:
    """Append ``callback`` to ``aqt.gui_hooks.<hook_name>`` (guarded unless it's a filter hook)."""
    registered = callback
    if hook_name not in _FILTER_HOOKS:
        registered = _guard(hook_name, callback)
        _GUARDED[(hook_name, callback)] = registered
    getattr(gui_hooks(), hook_name).append(registered)


def unsubscribe_hook(hook_name: str, callback: Callable[..., Any]) -> None:
    """Remove ``callback`` from ``aqt.gui_hooks.<hook_name>`` (safe if already gone)."""
    import contextlib

    registered = _GUARDED.pop((hook_name, callback), callback)
    hook = getattr(gui_hooks(), hook_name)
    with contextlib.suppress(ValueError):
        hook.remove(registered)


# --- Tools-menu action + shortcut (used by auto_flip's Ctrl+J toggle) ------------------
def add_tools_menu_action(
    label: str,
    callback: Callable[[bool], None],
    *,
    checkable: bool = False,
    checked: bool = False,
    shortcut: Optional[str] = None,
) -> Any:
    """Add a ``QAction`` to Anki's Tools menu and return it (for later removal).

    Args:
        label: The menu item text.
        callback: Invoked on trigger with the action's checked state (always ``False`` for
            a non-checkable action).
        checkable: Whether the action is a toggle.
        checked: Initial checked state when ``checkable``.
        shortcut: Optional key sequence (e.g. ``"Ctrl+J"``).

    Returns:
        The created ``QAction`` (pass it to :func:`remove_tools_menu_action` to tear down).
    """
    from aqt.qt import QAction, QKeySequence

    mw = main_window()
    action = QAction(label, mw)
    if checkable:
        action.setCheckable(True)
        action.setChecked(checked)
    if shortcut:
        action.setShortcut(QKeySequence(shortcut))
    action.triggered.connect(lambda *_a: callback(action.isChecked()))
    mw.form.menuTools.addAction(action)
    return action


def remove_tools_menu_action(action: Any) -> None:
    """Remove an action created by :func:`add_tools_menu_action` from the Tools menu."""
    if action is None:
        return
    main_window().form.menuTools.removeAction(action)


# --- Reviewer.onEnterKey wrap (used by auto_flip's two-stage Enter cancel) --------------
# (Reviewer class, original onEnterKey) so we can restore exactly what we replaced and never
# double-wrap if two callers (or a reload) install over each other.
_ENTER_KEY_ORIG: Optional[tuple[Any, Callable[..., Any]]] = None


def wrap_reviewer_enter_key(
    handler: Callable[[Any, Callable[[Any], None]], None],
) -> None:
    """Wrap ``Reviewer.onEnterKey`` so ``handler`` decides whether to act.

    ``handler`` is called as ``handler(reviewer, original)`` where ``original`` is a
    zero-extra-arg callable that performs Anki's real Enter action for that reviewer. This
    lets a feature intercept Enter (e.g. a first press cancels a pending timer, a second
    press calls ``original``) without the feature importing ``aqt`` or knowing the wrap
    mechanics. Idempotent: re-wrapping restores the original first so only one wrap is live.

    Args:
        handler: Receives ``(reviewer, original_enter_action)`` on each Enter press.
    """
    global _ENTER_KEY_ORIG
    from aqt.reviewer import Reviewer

    if _ENTER_KEY_ORIG is not None:
        restore_reviewer_enter_key()
    orig = Reviewer.onEnterKey
    _ENTER_KEY_ORIG = (Reviewer, orig)

    def onEnterKey(reviewer: Any) -> None:  # noqa: N802 (mirrors Anki's method name)
        handler(reviewer, lambda: orig(reviewer))

    Reviewer.onEnterKey = onEnterKey  # type: ignore[method-assign]


def restore_reviewer_enter_key() -> None:
    """Restore the original ``Reviewer.onEnterKey`` (safe if never wrapped)."""
    global _ENTER_KEY_ORIG
    if _ENTER_KEY_ORIG is None:
        return
    reviewer_cls, orig = _ENTER_KEY_ORIG
    reviewer_cls.onEnterKey = orig  # type: ignore[method-assign]
    _ENTER_KEY_ORIG = None
