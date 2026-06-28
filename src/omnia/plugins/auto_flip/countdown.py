"""Thin loader/formatter for the auto-flip reviewer countdown overlay JS.

The countdown JS body lives in ``gui/auto_flip/web/countdown.js`` (three ``// ===NAME===``
sections); these functions load it once, slice out the section they need, and substitute the
``__TOKEN__`` placeholders with JSON-encoded values. They return self-contained, CSP-safe
JavaScript (no external libraries) that the glue pushes into the reviewer webview via
``anki_compat.reviewer_eval``. Kept free of any ``aqt``/``anki`` imports so the strings can be
unit-tested headless.

The overlay is a fixed bottom-right badge: an SVG ring whose stroke shrinks as time runs
out, plus the remaining seconds. Each builder is idempotent for a given ``element_id`` —
re-injecting clears the previous interval/element first, so repeated show-question /
show-answer renders never stack timers.
"""

from __future__ import annotations

import json

import omnia.gui.auto_flip as _autoflip_gui
from omnia.gui.assets import read_asset

_DEFAULT_ELEMENT_ID = "omnia-autoflip-timer"

# Ring geometry (an SVG circle). The full circumference is the dash array; we grow the
# dash offset toward it as the remaining fraction drops, so the visible arc shrinks.
_RADIUS = 18
_CIRCUMFERENCE = 2 * 3.141592653589793 * _RADIUS

# ~10 ticks/sec: smooth enough for a countdown without spamming the webview.
_TICK_MS = 100

# The reference add-on recolors its bottom-bar timer blue when the user cancels a pending
# auto-flip with the first Enter press; the ring's "cancelled" state maps to that colour.
_CANCELLED_COLOR = "#3b82f6"


def _section(name: str) -> str:
    """Return the body of the ``// ===<name>===`` section of ``countdown.js``, trimmed."""
    text = read_asset(_autoflip_gui.__file__, "web", "countdown.js")
    blocks = text.split("// ===")
    for block in blocks[1:]:
        header, _, body = block.partition("\n")
        if header.strip() == f"{name}===":
            return body.strip()
    raise KeyError(f"countdown.js section not found: {name}")


def build_countdown_js(seconds: float, *, element_id: str = _DEFAULT_ELEMENT_ID) -> str:
    """Return JS that renders a countdown ring counting down from ``seconds`` to 0.

    The script clears any prior interval/element for ``element_id`` before starting, so it
    is safe to inject on every render. It updates roughly ten times a second.

    Args:
        seconds: Duration to count down from (the scheduled delay, in seconds).
        element_id: DOM id of the overlay element (lets callers run more than one).

    Returns:
        A self-contained JS snippet (no external dependencies).
    """
    return (
        _section("BUILD")
        .replace("__EID__", json.dumps(element_id))
        .replace("__TOTAL_MS__", json.dumps(max(0.0, float(seconds)) * 1000.0))
        .replace("__CIRCUMFERENCE__", repr(_CIRCUMFERENCE))
        .replace("__TICK_MS__", str(_TICK_MS))
        .replace("__RADIUS__", str(_RADIUS))
    )


def clear_countdown_js(element_id: str = _DEFAULT_ELEMENT_ID) -> str:
    """Return JS that clears the countdown interval and removes its overlay element.

    Args:
        element_id: DOM id of the overlay element to tear down.

    Returns:
        A self-contained JS snippet; a no-op in the webview if nothing is running.
    """
    return _section("CLEAR").replace("__EID__", json.dumps(element_id))


def mark_countdown_cancelled_js(element_id: str = _DEFAULT_ELEMENT_ID) -> str:
    """Return JS that freezes the countdown and recolors the ring as "cancelled".

    Stops the ticking interval (so the ring no longer shrinks) and paints the overlay in the
    cancelled colour, leaving it visible so the user sees the pending auto-flip was halted by
    their first Enter. The element is *not* removed — :func:`clear_countdown_js` does that on
    the next render.

    Args:
        element_id: DOM id of the overlay element to mark.

    Returns:
        A self-contained JS snippet; a no-op in the webview if nothing is running.
    """
    return (
        _section("CANCELLED")
        .replace("__EID__", json.dumps(element_id))
        .replace("__COLOR__", json.dumps(_CANCELLED_COLOR))
    )
