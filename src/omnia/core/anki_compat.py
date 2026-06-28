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
