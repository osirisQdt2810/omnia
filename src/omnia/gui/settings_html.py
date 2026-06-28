"""Pure HTML/CSS/JS builder for the Omnia settings page.

The settings dialog (``settings_dialog.py``) is thin Qt/webview glue; all of the page's
markup lives here as a pure, unit-testable function. Everything is inlined (no external
assets) because the host webview applies a strict CSP. The page talks back to Python via
the shared :class:`~omnia.gui.web_dialog.WebDialog` bridge with two ops:

* ``toggle`` ``{"id": <plugin_id>, "enabled": <bool>}`` → returns the new active state, so JS
  can reflect a failed enable.
* ``configure`` ``{"id": <plugin_id>}`` → opens the plugin's config dialog on the Qt side.

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class PluginCardModel:
    """View-model for one feature card (already resolved from a plugin + manager state)."""

    id: str
    name: str
    description: str
    tooltip: str
    enabled: bool
    active: bool
    configurable: bool


def status_text(*, enabled: bool, active: bool) -> str:
    """Human-readable status for a card: active, off, or a failed-enable warning."""
    if enabled and not active:
        return "failed to enable — see logs"
    return "active" if active else "off"


def build_settings_html(
    groups: list[tuple[str, list[PluginCardModel]]], *, dark: bool
) -> str:
    """Build the full settings page HTML.

    Args:
        groups: Sections as ``[(group_name, [PluginCardModel])]`` in display order.
        dark: Render the dark palette (Anki night mode) when True, else the light palette.

    Returns:
        A complete, self-contained HTML document string.
    """
    sections = "\n".join(_section_html(name, cards) for name, cards in groups)
    return _PAGE_TEMPLATE.format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=_CSS,
        sections=sections,
        js=_JS,
    )


def _section_html(group_name: str, cards: list[PluginCardModel]) -> str:
    rows = "\n".join(_card_html(card) for card in cards)
    return (
        '<section class="omnia-section">'
        f'<h2 class="omnia-section-label">{html.escape(group_name)}</h2>'
        f'<div class="omnia-cards">{rows}</div>'
        "</section>"
    )


def _card_html(card: PluginCardModel) -> str:
    tip = card.tooltip or card.description
    checked = " checked" if card.enabled else ""
    failed = " omnia-failed" if (card.enabled and not card.active) else ""
    configure = (
        f'<button class="omnia-configure" data-id="{html.escape(card.id)}">Configure…</button>'
        if card.configurable
        else ""
    )
    return (
        f'<div class="omnia-card{failed}" data-id="{html.escape(card.id)}" '
        f'title="{html.escape(tip)}">'
        '<div class="omnia-card-text">'
        f'<div class="omnia-card-title">{html.escape(card.name or card.id)}</div>'
        f'<div class="omnia-card-desc">{html.escape(card.description)}</div>'
        f'<div class="omnia-card-status">{html.escape(status_text(enabled=card.enabled, active=card.active))}</div>'
        "</div>"
        '<div class="omnia-card-actions">'
        f"{configure}"
        '<label class="omnia-switch" title="'
        f'{html.escape(tip)}">'
        f'<input type="checkbox" data-id="{html.escape(card.id)}"{checked}>'
        '<span class="omnia-slider"></span>'
        "</label>"
        "</div>"
        "</div>"
    )


# The whole document. The CSP forbids external assets; everything is inline. Curly braces in
# the CSS/JS are doubled so ``str.format`` only fills the named placeholders.
_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body class="{theme_class}">
<div class="omnia-shell">
  <header class="omnia-header">
    <div class="omnia-title">Omnia — All-in-One Toolkit</div>
    <div class="omnia-subtitle">Tick a feature to turn it on — changes apply immediately.</div>
  </header>
  <main class="omnia-body">
{sections}
  </main>
</div>
<script>{js}</script>
</body>
</html>"""


# Light/dark are driven by a body class so the same CSS file adapts to Anki's theme without
# hard-coding a dark-only palette. Custom properties hold the per-theme colors.
_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
body {
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
}
body.omnia-light {
  --bg-top: #f7f8fb; --bg-bottom: #eef1f7; --fg: #1d2230; --muted: #6b7280;
  --card-top: rgba(255,255,255,0.92); --card-bottom: rgba(246,248,252,0.88);
  --card-border: rgba(20,30,60,0.10); --accent: #5b6ef5; --accent-2: #8a5cf6;
  --switch-off: rgba(20,30,60,0.18); --shadow: rgba(20,30,60,0.12); --fail: #c0392b;
}
body.omnia-dark {
  --bg-top: #1b1e27; --bg-bottom: #14161d; --fg: #e7e9f0; --muted: #9aa3b2;
  --card-top: rgba(46,51,66,0.65); --card-bottom: rgba(32,36,48,0.55);
  --card-border: rgba(255,255,255,0.08); --accent: #7c8cff; --accent-2: #a685ff;
  --switch-off: rgba(255,255,255,0.18); --shadow: rgba(0,0,0,0.45); --fail: #ff6b6b;
}
body {
  color: var(--fg);
  background: linear-gradient(160deg, var(--bg-top), var(--bg-bottom));
}
.omnia-shell { max-width: 720px; margin: 0 auto; padding: 0 18px 24px; }
.omnia-header {
  position: sticky; top: 0; z-index: 5; padding: 22px 4px 14px;
  background: linear-gradient(160deg, var(--bg-top), var(--bg-bottom));
}
.omnia-title {
  font-size: 22px; font-weight: 800; letter-spacing: 0.2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.omnia-subtitle { color: var(--muted); margin-top: 4px; }
.omnia-section { margin-top: 18px; }
.omnia-section-label {
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--muted); margin: 0 0 8px 2px;
}
.omnia-cards { display: flex; flex-direction: column; gap: 10px; }
.omnia-card {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 14px; border-radius: 14px;
  border: 1px solid var(--card-border);
  background: linear-gradient(150deg, var(--card-top), var(--card-bottom));
  box-shadow: 0 1px 2px var(--shadow);
  transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
}
.omnia-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px var(--shadow);
  border-color: var(--accent);
}
.omnia-card.omnia-failed { border-color: var(--fail); }
.omnia-card-text { flex: 1; min-width: 0; }
.omnia-card-title { font-size: 15px; font-weight: 650; }
.omnia-card-desc { color: var(--muted); margin-top: 2px; line-height: 1.35; }
.omnia-card-status { font-size: 11px; color: var(--muted); margin-top: 6px; }
.omnia-card.omnia-failed .omnia-card-status { color: var(--fail); font-weight: 600; }
.omnia-card-actions { display: flex; align-items: center; gap: 10px; }
.omnia-configure {
  border: 1px solid var(--card-border); border-radius: 9px; cursor: pointer;
  padding: 6px 12px; font-size: 13px; color: var(--fg);
  background: var(--card-top);
  transition: border-color 0.15s ease, transform 0.1s ease;
}
.omnia-configure:hover { border-color: var(--accent); }
.omnia-configure:active { transform: scale(0.97); }
.omnia-switch { position: relative; display: inline-block; width: 46px; height: 26px; }
.omnia-switch input { opacity: 0; width: 0; height: 0; }
.omnia-slider {
  position: absolute; cursor: pointer; inset: 0; border-radius: 999px;
  background: var(--switch-off); transition: background 0.25s ease;
}
.omnia-slider::before {
  content: ""; position: absolute; height: 20px; width: 20px; left: 3px; top: 3px;
  border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  transition: transform 0.25s cubic-bezier(0.4, 0.0, 0.2, 1);
}
.omnia-switch input:checked + .omnia-slider {
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
}
.omnia-switch input:checked + .omnia-slider::before { transform: translateX(20px); }
"""


# All UI events post one of two ops back to Python; ``toggle`` returns the resulting active
# state so a failed enable is reflected (the switch snaps back + the card shows the error).
_JS = """
(function () {
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({ plugin: "settings", op: op, data: data }), cb);
  }
  function setStatus(card, text, failed) {
    var s = card.querySelector(".omnia-card-status");
    if (s) s.textContent = text;
    card.classList.toggle("omnia-failed", !!failed);
  }
  document.querySelectorAll(".omnia-switch input").forEach(function (input) {
    input.addEventListener("change", function () {
      var card = input.closest(".omnia-card");
      var id = input.getAttribute("data-id");
      var enabled = input.checked;
      send("toggle", { id: id, enabled: enabled }, function (res) {
        var active = !!(res && res.active);
        if (enabled && !active) {
          input.checked = false;
          setStatus(card, "failed to enable — see logs", true);
        } else {
          setStatus(card, active ? "active" : "off", false);
        }
      });
    });
  });
  document.querySelectorAll(".omnia-configure").forEach(function (btn) {
    btn.addEventListener("click", function () {
      send("configure", { id: btn.getAttribute("data-id") }, null);
    });
  });
})();
"""
