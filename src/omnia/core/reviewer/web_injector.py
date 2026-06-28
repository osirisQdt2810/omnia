"""Reviewer web seam: inject JS/CSS into the reviewer webview and route the ``pycmd`` bridge.

Features register a :class:`WebAsset` (CSS + per-side JS) and/or ``pycmd`` op handlers
through this single object instead of calling ``web.eval`` or appending to
``webview_did_receive_js_message`` themselves.

Bridge message format (one JSON object after a fixed prefix)::

    omnia:{"plugin": "<plugin_id>", "op": "<op>", "data": {...}}

The parsing/routing logic (:func:`parse_message`, :class:`MessageRouter`) is pure and
unit-tested; :class:`WebInjector` is the thin Anki glue.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

MESSAGE_PREFIX = "omnia:"

# A pycmd op handler: (data, context) -> result. The result is returned to JS.
PycmdHandler = Callable[[dict[str, Any], Any], Any]

# A per-card dynamic JS provider: (card) -> JS string to eval (or None for nothing).
DynamicJs = Callable[[Any], Optional[str]]


@dataclass
class WebAsset:
    """CSS + per-side JavaScript a plugin injects into the reviewer."""

    css: str = ""
    question_js: str = ""
    answer_js: str = ""


@dataclass(frozen=True)
class ParsedMessage:
    """A decoded bridge message."""

    plugin: str
    op: str
    data: dict[str, Any]


def parse_message(message: str) -> Optional[ParsedMessage]:
    """Parse an ``omnia:{...}`` bridge message, or return None if it isn't ours/invalid."""
    if not message.startswith(MESSAGE_PREFIX):
        return None
    raw = message[len(MESSAGE_PREFIX) :]
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    plugin = payload.get("plugin")
    op = payload.get("op")
    if not isinstance(plugin, str) or not isinstance(op, str):
        return None
    data = payload.get("data")
    return ParsedMessage(plugin, op, data if isinstance(data, dict) else {})


def build_message(plugin: str, op: str, data: Optional[dict[str, Any]] = None) -> str:
    """Build a bridge message string (mirror of :func:`parse_message`, handy for tests/JS docs)."""
    return MESSAGE_PREFIX + json.dumps({"plugin": plugin, "op": op, "data": data or {}})


class MessageRouter:
    """Pure registry mapping ``(plugin, op)`` to a handler, with dispatch."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], PycmdHandler] = {}

    def register(self, plugin: str, op: str, handler: PycmdHandler) -> None:
        """Register ``handler`` for ``(plugin, op)``."""
        self._handlers[(plugin, op)] = handler

    def unregister_plugin(self, plugin: str) -> None:
        """Remove every handler belonging to ``plugin``."""
        for key in [k for k in self._handlers if k[0] == plugin]:
            del self._handlers[key]

    def dispatch(self, message: str, context: Any) -> tuple[bool, Any]:
        """Route ``message``; return ``(handled, result)``.

        ``handled`` is False for non-Omnia or unrouted messages so the caller can pass the
        original ``webview_did_receive_js_message`` tuple through untouched.
        """
        parsed = parse_message(message)
        if parsed is None:
            return (False, None)
        handler = self._handlers.get((parsed.plugin, parsed.op))
        if handler is None:
            return (False, None)
        return (True, handler(parsed.data, context))


class WebInjector:
    """Owns reviewer asset injection and the single ``pycmd`` bridge hook."""

    def __init__(self) -> None:
        self._assets: dict[str, WebAsset] = {}
        # Per-card dynamic JS providers: plugin_id -> {"question"|"answer": (card) -> str|None}.
        self._dynamic: dict[str, dict[str, DynamicJs]] = {}
        self._router = MessageRouter()
        self._installed = False

    # --- registration (used by feature plugins via the context) --------------------
    def add_asset(self, plugin_id: str, asset: WebAsset) -> None:
        """Register (or replace) ``plugin_id``'s reviewer asset."""
        self._assets[plugin_id] = asset

    def add_dynamic(
        self,
        plugin_id: str,
        *,
        on_question: Optional[DynamicJs] = None,
        on_answer: Optional[DynamicJs] = None,
    ) -> None:
        """Register per-card JS providers; each ``(card) -> str|None`` runs at show time.

        Use this when the JS depends on the specific card (e.g. an interval overlay).
        Returning None or "" injects nothing for that card.
        """
        self._dynamic[plugin_id] = {"question": on_question, "answer": on_answer}

    def add_handler(self, plugin_id: str, op: str, handler: PycmdHandler) -> None:
        """Register a ``pycmd`` handler for ``plugin_id``'s ``op``."""
        self._router.register(plugin_id, op, handler)

    def remove(self, plugin_id: str) -> None:
        """Remove a plugin's asset, dynamic providers, and handlers (on disable)."""
        self._assets.pop(plugin_id, None)
        self._dynamic.pop(plugin_id, None)
        self._router.unregister_plugin(plugin_id)

    # --- pure helpers ---------------------------------------------------------------
    def collect_js(self, side: str) -> str:
        """Return the concatenated CSS + JS to eval for ``side`` ('question'|'answer')."""
        if side not in ("question", "answer"):
            raise ValueError(f"side must be 'question' or 'answer', got {side!r}")
        chunks: list[str] = []
        for plugin_id, asset in self._assets.items():
            if asset.css:
                chunks.append(_inject_css_js(plugin_id, asset.css))
            side_js = asset.question_js if side == "question" else asset.answer_js
            if side_js:
                chunks.append(side_js)
        return "\n".join(chunks)

    # --- Anki glue ------------------------------------------------------------------
    def install(self) -> None:
        """Attach the reviewer show hooks and the bridge hook. Idempotent."""
        if self._installed:
            return
        from omnia.core.anki_compat import gui_hooks

        hooks = gui_hooks()
        hooks.reviewer_did_show_question.append(self._on_show_question)
        hooks.reviewer_did_show_answer.append(self._on_show_answer)
        hooks.webview_did_receive_js_message.append(self._on_js_message)
        self._installed = True

    def uninstall(self) -> None:
        """Detach the reviewer hooks (on add-on teardown / profile close). Idempotent."""
        if not self._installed:
            return
        from omnia.core.anki_compat import gui_hooks

        hooks = gui_hooks()
        hooks.reviewer_did_show_question.remove(self._on_show_question)
        hooks.reviewer_did_show_answer.remove(self._on_show_answer)
        hooks.webview_did_receive_js_message.remove(self._on_js_message)
        self._installed = False

    def _dynamic_js(self, side: str, card: Any) -> str:
        """Collect per-card JS from the dynamic providers for ``side``."""
        chunks: list[str] = []
        for providers in self._dynamic.values():
            provider = providers.get(side)
            if provider is None:
                continue
            out = provider(card)
            if out:
                chunks.append(out)
        return "\n".join(chunks)

    def _eval(self, side: str, card: Any) -> None:
        # Resilience: a bug in any plugin's JS/dynamic provider must NOT crash the reviewer
        # (this fires on every show-question/answer). Log it and move on.
        try:
            js = "\n".join(
                filter(None, (self.collect_js(side), self._dynamic_js(side, card)))
            )
            if not js:
                return
            from omnia.core.anki_compat import main_window

            web = getattr(main_window().reviewer, "web", None)
            if web is not None:
                web.eval(js)
        except Exception:
            from omnia.core.logging import get_logger

            get_logger().exception("web injector: %s-side eval failed", side)

    def _on_show_question(self, card: Any) -> None:
        self._eval("question", card)

    def _on_show_answer(self, card: Any) -> None:
        self._eval("answer", card)

    def _on_js_message(
        self, handled: tuple[bool, Any], message: str, context: Any
    ) -> tuple[bool, Any]:
        # This filter fires on EVERY pycmd from any webview — a handler bug here would break
        # every click. Isolate failures: log and pass the message through untouched.
        try:
            ours, result = self._router.dispatch(message, context)
        except Exception:
            from omnia.core.logging import get_logger

            get_logger().exception("web injector: pycmd dispatch failed (%r)", message)
            return handled
        return (True, result) if ours else handled


def _inject_css_js(plugin_id: str, plugin_css: str) -> str:
    """Build a JS snippet that injects ``plugin_css`` once, in a per-plugin <style> element."""
    element_id = json.dumps(f"omnia-style-{plugin_id}")
    style = json.dumps(plugin_css)
    # Idempotent per plugin: each plugin owns its own <style>, reused across renders.
    return (
        "(function(){var id=" + element_id + ";var s=document.getElementById(id);"
        "if(!s){s=document.createElement('style');s.id=id;"
        "document.head.appendChild(s);}s.textContent=" + style + ";})();"
    )
