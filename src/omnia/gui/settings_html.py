"""Pure HTML/CSS/JS builder for the Omnia settings page.

The settings dialog (``settings_dialog.py``) is thin Qt/webview glue; all of the page's
markup lives in sibling asset files (``settings.html`` / ``settings.css`` / ``settings.js``)
and is assembled here by a pure, unit-testable function. Everything is inlined into one
document (no external <link>/<script src>) because the host webview applies a strict CSP.
The page talks back to Python via the shared :class:`~omnia.gui.web_dialog.WebDialog` bridge
with two ops:

* ``toggle`` ``{"id": <plugin_id>, "enabled": <bool>}`` → returns the new active state, so JS
  can reflect a failed enable.
* ``configure`` ``{"id": <plugin_id>}`` → opens the plugin's config dialog on the Qt side.

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from omnia.gui.assets import read_asset


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
    return read_asset(__file__, "settings.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "settings.css"),
        sections=sections,
        js=read_asset(__file__, "settings.js"),
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
