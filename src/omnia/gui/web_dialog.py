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

from aqt.qt import QCloseEvent, QDialog, Qt, QVBoxLayout, QWebEngineView, QWidget
from aqt.webview import AnkiWebView

from omnia.core.logging import get_logger
from omnia.core.reviewer.web_injector import parse_message

logger = get_logger()

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
        # Delete on close so the dialog + its webview are reclaimed instead of lingering.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._web_cleaned = False
        self._handlers = handlers
        self.setWindowTitle(title)
        self.setMinimumSize(width, height)

        self._web = AnkiWebView(self)
        # A standalone settings dialog renders self-contained HTML and needs no collection;
        # without this the webview's bridge guard dereferences ``mw.col`` and crashes.
        self._web.requiresCol = False
        self._web.set_bridge_command(self._on_cmd, self)
        # Diagnostic: log whether the page actually loaded + how much body content it has, so a
        # "blank dialog" report from a real (GPU) Anki — which the offscreen test harness can't
        # reproduce — is diagnosable from omnia.log alone (did it not render, or not paint?).
        self._web.loadFinished.connect(self._on_load_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._web)

        self.set_html(html)

    def closeEvent(self, evt: QCloseEvent) -> None:  # noqa: N802 (Qt override name)
        """Tear the hosted webview down on close so it doesn't leak.

        Each :class:`AnkiWebView` allocates a ``QWebEnginePage`` and appends two global
        ``gui_hooks`` callbacks; without ``cleanup()`` every open leaks a page and grows those
        hook lists. Cleanup runs exactly once (guarded), then the dialog schedules its own
        deletion.
        """
        self._cleanup_web()
        super().closeEvent(evt)
        self.deleteLater()

    def _cleanup_web(self) -> None:
        """Call ``AnkiWebView.cleanup`` exactly once (safe if the webview is missing/gone)."""
        web = getattr(self, "_web", None)
        if web is not None and not self._web_cleaned:
            self._web_cleaned = True
            web.cleanup()

    def _on_load_finished(self, ok: bool) -> None:
        """Log the page-load outcome + rendered body size (real-Anki blank-dialog diagnostic)."""
        title = self.windowTitle()
        logger.info("WebDialog %r loadFinished ok=%s", title, ok)

        def _probe(result: Any) -> None:
            logger.info("WebDialog %r body-probe: %s", title, result)

        try:
            self._web.evalWithCallback(
                "(function(){var b=document.body;"
                "return b?(b.innerHTML.length+' chars, '+b.children.length+' children'):"
                "'no body';})()",
                _probe,
            )
        except Exception:  # diagnostic only — never let it affect the dialog
            logger.exception("WebDialog %r body-probe failed", title)

    def set_html(self, html: str) -> None:
        """Render ``html`` in the webview.

        Prefer ``AnkiWebView.setHtml`` (serves via Anki's media server) — that is the ONLY
        path that wires the ``pycmd`` *callback* round-trip, so a handler's return value
        actually reaches the JS ``cb``. A page that populates itself via callbacks
        (``list_note_types``/``load``) is blank without it. Fall back to the base
        ``QWebEngineView.setHtml`` only when there's no media server (e.g. the headless test
        stub), where the page still renders but callbacks are inert.
        """
        from aqt import mw

        if getattr(mw, "mediaServer", None) is not None:
            self._web.setHtml(html)
        else:
            QWebEngineView.setHtml(self._web, html)

    def eval_js(self, js: str) -> None:
        """Evaluate ``js`` in the hosted webview.

        For pushing a result the page can't receive through the synchronous ``pycmd`` callback
        (e.g. an op whose work runs off the Qt main thread and reports back later).

        A late off-thread callback can fire after the dialog is closed (cleanup +
        ``deleteLater``); calling ``eval`` on the deleted C++ webview would crash. Guard on both
        our cleanup flag and sip's liveness check before touching the webview.
        """
        from aqt.qt import sip

        if self._web_cleaned or sip.isdeleted(self._web):
            return
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
            logger.exception("WebDialog handler for op %r failed", parsed.op)
            return None
