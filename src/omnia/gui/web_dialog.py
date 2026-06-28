"""A reusable webview-hosting dialog seam.

:class:`WebDialog` is a plain ``QDialog`` whose entire surface is an
:class:`aqt.webview.AnkiWebView` rendering caller-supplied, fully self-contained HTML
(inline CSS/JS — a strict CSP forbids external assets). The page talks back to Python
through the webview's ``pycmd`` bridge using Omnia's envelope::

    pycmd('omnia:{"plugin": "<ns>", "op": "<op>", "data": {...}}', cb)

The dialog routes each message to a handler from the ``handlers`` map (``op -> handler``);
the handler's return value (any JSON-serializable object) resolves the JS-side ``cb``.

This is the shared host for the settings UI and, later, the Smart Notes dialog. Pure Qt
glue — only loaded inside Anki; the message parsing lives in the pure
``core.reviewer.web_injector`` module it reuses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aqt.qt import QDialog, QVBoxLayout, QWebEngineView, QWidget
from aqt.webview import AnkiWebView

from omnia.core.logging import get_logger
from omnia.core.reviewer.web_injector import parse_message

# A WebDialog op handler: (data) -> JSON-serializable result returned to the JS callback.
DialogHandler = Callable[[dict[str, Any]], Any]


class WebDialog(QDialog):
    """A ``QDialog`` that hosts an ``AnkiWebView`` and routes its ``pycmd`` messages.

    Args:
        parent: The Qt parent widget (or None).
        title: The window title.
        html: A complete, self-contained HTML document (inline CSS/JS).
        handlers: Map of ``op`` name to ``handler(data) -> result``. The result is returned
            to the JS ``pycmd`` callback (serialized by Anki's bridge). A handler raising is
            logged via the Omnia logger and never crashes the dialog.
        width / height: Minimum dialog size.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        html: str,
        handlers: dict[str, DialogHandler],
        width: int = 640,
        height: int = 560,
    ) -> None:
        super().__init__(parent)
        self._handlers = handlers
        self._log = get_logger()
        self.setWindowTitle(title)
        self.setMinimumSize(width, height)

        self._web = AnkiWebView(self)
        # A standalone settings dialog renders self-contained HTML and needs no collection;
        # without this the webview's bridge guard dereferences ``mw.col`` and crashes.
        self._web.requiresCol = False
        self._web.set_bridge_command(self._on_cmd, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._web)

        self.set_html(html)

    def set_html(self, html: str) -> None:
        """Render ``html`` in the webview.

        Renders through the base ``QWebEngineView.setHtml`` (not ``AnkiWebView.setHtml``)
        because the page is fully self-contained — that path avoids the media server and works
        the same headless and inside Anki.
        """
        QWebEngineView.setHtml(self._web, html)

    def eval_js(self, js: str) -> None:
        """Evaluate ``js`` in the hosted webview.

        For pushing a result the page can't receive through the synchronous ``pycmd`` callback
        (e.g. an op whose work runs off the Qt main thread and reports back later).
        """
        self._web.eval(js)

    def _on_cmd(self, message: str) -> Any:
        """Bridge entry point: route an ``omnia:`` envelope to its ``op`` handler.

        Returns the handler's result (serialized to the JS callback) or None for messages we
        don't own / can't parse. A handler exception is logged and swallowed so a UI action
        can never crash the dialog.
        """
        parsed = parse_message(message)
        if parsed is None:
            return None
        handler = self._handlers.get(parsed.op)
        if handler is None:
            return None
        try:
            return handler(parsed.data)
        except Exception:  # UI boundary: never let a handler crash the dialog
            self._log.exception("WebDialog handler for op %r failed", parsed.op)
            return None
