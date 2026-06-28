"""Pure JS builders for the auto-flip reviewer countdown overlay.

These return self-contained, CSP-safe JavaScript (no external libraries) that the glue
pushes into the reviewer webview via ``anki_compat.reviewer_eval``. Kept free of any
``aqt``/``anki`` imports so the strings can be unit-tested headless.

The overlay is a fixed bottom-right badge: an SVG ring whose stroke shrinks as time runs
out, plus the remaining seconds. Each builder is idempotent for a given ``element_id`` —
re-injecting clears the previous interval/element first, so repeated show-question /
show-answer renders never stack timers.
"""

from __future__ import annotations

import json

_DEFAULT_ELEMENT_ID = "omnia-autoflip-timer"

# Ring geometry (an SVG circle). The full circumference is the dash array; we grow the
# dash offset toward it as the remaining fraction drops, so the visible arc shrinks.
_RADIUS = 18
_CIRCUMFERENCE = 2 * 3.141592653589793 * _RADIUS

# ~10 ticks/sec: smooth enough for a countdown without spamming the webview.
_TICK_MS = 100


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
    eid = json.dumps(element_id)
    total_ms = json.dumps(max(0.0, float(seconds)) * 1000.0)
    return f"""(function() {{
    var id = {eid};
    var totalMs = {total_ms};
    var circumference = {_CIRCUMFERENCE!r};
    var tickMs = {_TICK_MS};
    if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {{
        clearInterval(window.__omniaAutoflipTimers[id]);
    }}
    if (!window.__omniaAutoflipTimers) {{ window.__omniaAutoflipTimers = {{}}; }}
    var existing = document.getElementById(id);
    if (existing) {{ existing.parentNode.removeChild(existing); }}
    var box = document.createElement("div");
    box.id = id;
    box.style.cssText = "position:fixed;right:16px;bottom:16px;width:48px;height:48px;" +
        "z-index:2147483647;pointer-events:none;font:600 12px sans-serif;color:#888;";
    box.innerHTML =
        '<svg width="48" height="48" viewBox="0 0 48 48" style="position:absolute;' +
        'top:0;left:0;transform:rotate(-90deg);">' +
        '<circle cx="24" cy="24" r="{_RADIUS}" fill="none" stroke="rgba(128,128,128,0.25)"' +
        ' stroke-width="4"></circle>' +
        '<circle class="omnia-ring" cx="24" cy="24" r="{_RADIUS}" fill="none"' +
        ' stroke="currentColor" stroke-width="4" stroke-linecap="round"' +
        ' stroke-dasharray="' + circumference + '" stroke-dashoffset="0"></circle>' +
        '</svg>' +
        '<span class="omnia-secs" style="position:absolute;top:0;left:0;width:48px;' +
        'height:48px;display:flex;align-items:center;justify-content:center;"></span>';
    document.body.appendChild(box);
    var ring = box.querySelector(".omnia-ring");
    var label = box.querySelector(".omnia-secs");
    var start = Date.now();
    function render() {{
        var elapsed = Date.now() - start;
        var remaining = Math.max(0, totalMs - elapsed);
        var fraction = totalMs > 0 ? remaining / totalMs : 0;
        if (ring) {{ ring.setAttribute("stroke-dashoffset", circumference * (1 - fraction)); }}
        if (label) {{ label.textContent = (remaining / 1000).toFixed(1); }}
        if (remaining <= 0) {{
            clearInterval(window.__omniaAutoflipTimers[id]);
            delete window.__omniaAutoflipTimers[id];
        }}
    }}
    render();
    window.__omniaAutoflipTimers[id] = setInterval(render, tickMs);
}})();"""


def clear_countdown_js(element_id: str = _DEFAULT_ELEMENT_ID) -> str:
    """Return JS that clears the countdown interval and removes its overlay element.

    Args:
        element_id: DOM id of the overlay element to tear down.

    Returns:
        A self-contained JS snippet; a no-op in the webview if nothing is running.
    """
    eid = json.dumps(element_id)
    return f"""(function() {{
    var id = {eid};
    if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {{
        clearInterval(window.__omniaAutoflipTimers[id]);
        delete window.__omniaAutoflipTimers[id];
    }}
    var el = document.getElementById(id);
    if (el) {{ el.parentNode.removeChild(el); }}
}})();"""


# The reference add-on recolors its bottom-bar timer blue when the user cancels a pending
# auto-flip with the first Enter press; the ring's "cancelled" state maps to that colour.
_CANCELLED_COLOR = "#3b82f6"


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
    eid = json.dumps(element_id)
    color = json.dumps(_CANCELLED_COLOR)
    return f"""(function() {{
    var id = {eid};
    if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {{
        clearInterval(window.__omniaAutoflipTimers[id]);
        delete window.__omniaAutoflipTimers[id];
    }}
    var el = document.getElementById(id);
    if (!el) {{ return; }}
    el.style.color = {color};
    var ring = el.querySelector(".omnia-ring");
    if (ring) {{ ring.setAttribute("stroke-dashoffset", 0); }}
}})();"""
