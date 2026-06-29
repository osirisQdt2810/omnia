"""Inject the interactive typed-accuracy panel into Anki's Statistics screen.

Reads the bundled web assets (CSS + HTML template + the ordered JS pieces) and ``eval``s them
into the stats webview, mirroring the reference's ``stats_injector``: the CSS is installed
into a ``<style>`` element, the HTML template is stashed on ``window.__TA_HTML_TEMPLATE`` for
the JS to mount, then the JS runs (and gates itself to the stats DOM). The assets live in a
``web/`` folder next to the feature's GUI module, so no Anki import is needed to read them —
only ``webview.eval`` is Anki glue, and that webview is passed in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnia.core.logging import get_logger

logger = get_logger("typed_accuracy")

_CSS_STYLE_ID = "typed-accuracy-stats-css"

# The panel script is split into cohesive pieces; they are read in this exact order and
# joined with ``"\n"`` to reproduce the original single ``typed_accuracy.js`` byte-for-byte.
_JS_PARTS = (
    "01-bridge.js",
    "02-format.js",
    "03-donut.js",
    "04-table.js",
    "05-boot.js",
)


class StatsInjector:
    """Loads the panel assets and evaluates them into a stats webview."""

    def __init__(self, web_dir: Path) -> None:
        """Initialise the injector.

        Args:
            web_dir: Directory holding ``typed_accuracy.{css,html}`` plus the ordered
                ``0N-*.js`` script pieces.
        """
        self._web_dir = web_dir

    def _read(self, name: str) -> str:
        return (self._web_dir / name).read_text(encoding="utf-8")

    def _read_js(self) -> str:
        return "\n".join(self._read(name) for name in _JS_PARTS)

    def inject(self, webview: Any) -> None:
        """Install the CSS, stash the HTML template, and run the panel JS in ``webview``.

        Resilient: a missing asset or eval error is logged, never raised — this fires on the
        stats screen and must not break it.
        """
        try:
            css = self._read("typed_accuracy.css")
            html = self._read("typed_accuracy.html")
            js = self._read_js()
        except OSError:
            logger.exception("typed_accuracy: failed to read panel assets")
            return

        try:
            webview.eval(f"""
(function(){{
  try {{
    var id = {json.dumps(_CSS_STYLE_ID)};
    var el = document.getElementById(id);
    if (!el) {{
      el = document.createElement("style");
      el.id = id;
      document.head.appendChild(el);
    }}
    el.textContent = {json.dumps(css)};
  }} catch(e) {{}}
}})();
""")
            webview.eval(f"""
(function(){{
  try {{
    window.__TA_HTML_TEMPLATE = {json.dumps(html)};
  }} catch(e) {{}}
}})();
""")
            webview.eval(js)
        except Exception:
            logger.exception("typed_accuracy: panel injection failed")
