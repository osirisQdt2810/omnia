"""The Cultivation page's markup: a shell the script fills from the status cards.

The page draws itself from JSON (``window.cultivation.update(domain, card)``) rather than being
re-rendered from Python, because both domains refresh independently — the Anki card arrives from
a background read, the DSA card whenever dsa-journey rewrites its feed — and a whole-page reload
would throw away a trial in progress.

Pure string building, no ``aqt`` — so the page unit-tests headless.
"""

from __future__ import annotations

from omnia.gui.assets import read_asset
from omnia.plugins.cultivation.realms import REALM_NAMES


def build_cultivation_html(*, dark: bool) -> str:
    """Build the complete Cultivation page."""
    import json

    return read_asset(__file__, "web", "cultivation.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "web", "cultivation.css"),
        realms=json.dumps(list(REALM_NAMES), ensure_ascii=False),
        js=read_asset(__file__, "web", "cultivation.js"),
    )
