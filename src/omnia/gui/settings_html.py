"""Pure HTML/CSS/JS builder for the Omnia settings page.

The settings dialog (``settings_dialog.py``) is thin Qt/webview glue; all of the page's
markup lives in asset files under the sibling ``web/`` folder (``web/settings.html`` /
``web/settings.css`` / ``web/settings.js``) and is assembled here by a pure, unit-testable
function. Everything is inlined into one document (no external <link>/<script src>) because
the host webview applies a strict CSP.

The page has TWO views inside that one document: a landing grid of category tiles, and one
detail view per category holding that category's feature cards. Every view is rendered up
front and all but one is ``hidden``; switching between them is client-side JS, so opening a
category costs no round trip and needs no extra ``pycmd`` op. The page still talks back to
Python through the shared :class:`~omnia.gui.web_dialog.WebDialog` bridge with the same two:

* ``toggle`` ``{"id": <plugin_id>, "enabled": <bool>}`` → returns the new active state, so JS
  can reflect a failed enable.
* ``configure`` ``{"id": <plugin_id>}`` → opens the plugin's config dialog on the Qt side.

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

from omnia.gui.assets import read_asset
from omnia.gui.settings_categories import CategoryStyle, category_style

_NON_SLUG = re.compile(r"[^a-z0-9]+")


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


@dataclass(frozen=True)
class CategoryModel:
    """View-model for one landing tile and the detail view it opens.

    Attributes:
        key: DOM-safe unique handle (``"<slug>-<index>"``), used as the ``data-category``
            value on both the tile and its section so the JS can pair them.
        name: The raw group name, escaped at render time.
        style: How the category is painted (icon, blurb, gradient).
        cards: The category's feature cards, already in display order.
    """

    key: str
    name: str
    style: CategoryStyle
    cards: list[PluginCardModel]

    @property
    def total(self) -> int:
        """How many features the category holds."""
        return len(self.cards)

    @property
    def on_count(self) -> int:
        """Cards whose switch renders CHECKED — i.e. ``card.enabled``.

        Deliberately NOT ``enabled and active``: the JS recomputes this straight from the
        checked inputs after every toggle, so seeding it from the same fact keeps one source
        of truth and lets a failed enable self-correct (the handler unchecks the input first,
        then recounts).
        """
        return sum(1 for card in self.cards if card.enabled)


def status_text(*, enabled: bool, active: bool) -> str:
    """Human-readable status for a card: active, off, or a failed-enable warning."""
    if enabled and not active:
        return "failed to enable — see logs"
    return "active" if active else "off"


def category_models(
    groups: list[tuple[str, list[PluginCardModel]]],
) -> list[CategoryModel]:
    """Resolve grouped cards into ordered category view-models.

    Args:
        groups: Sections as ``[(group_name, [PluginCardModel])]`` in display order (what
            :func:`omnia.core.manager.group_plugins` produces).

    Returns:
        One :class:`CategoryModel` per group, in the same order, each carrying the group's
        presentation style (an unlisted group gets the default one).
    """
    return [
        CategoryModel(
            key=_slug(name, index),
            name=name,
            style=category_style(name),
            cards=cards,
        )
        for index, (name, cards) in enumerate(groups)
    ]


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
    categories = category_models(groups)
    views = "\n".join(_category_view_html(category) for category in categories)
    # ``web/settings.html`` is consumed by str.format, so it must never contain a literal
    # brace of its own. The CSS and JS arrive as VALUES, which is why their braces are fine.
    return read_asset(__file__, "web", "settings.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "web", "settings.css"),
        actions=_header_actions_html(),
        landing=_landing_html(categories),
        categories=views,
        js=read_asset(__file__, "web", "settings.js"),
    )


def category_key(group_name: str, rendered: Sequence[str]) -> str:
    """The ``data-category`` handle for a group, as the rendered page spelled it.

    The handle carries the group's POSITION, which is why this takes the groups the page
    ACTUALLY rendered rather than the configured category order. Those two are not the same
    list: ``group_plugins`` drops a group with no plugins in it and skips always-on ones
    entirely, so a configured order of five names can render as four sections — and every index
    after the gap shifts. Deriving the handle from the configured order would then point Back at
    a category that is not there, on exactly the installs where some feature happens to be
    absent.

    Args:
        group_name: The plugin's ``group``.
        rendered: The group names the page was built from, in order.

    Returns:
        The handle, e.g. ``"ai-2"``. An unrendered group gets the end of the list rather than
        an exception; nothing can open its panel anyway, since it has no card to press.
    """
    names = list(rendered)
    index = names.index(group_name) if group_name in names else len(names)
    return _slug(group_name, index)


def _slug(name: str, index: int) -> str:
    """Turn a group name into a DOM-safe handle: ``("AI", 2)`` → ``"ai-2"``.

    The trailing index makes the handle unique by construction (no dedupe pass needed when
    two group names slugify the same), and restricting the charset to ``[a-z0-9-]`` is what
    lets the JS build a ``[data-category="…"]`` selector without escaping a group name that
    could otherwise contain a quote.
    """
    slug = _NON_SLUG.sub("-", name.lower()).strip("-")
    return f"{slug or 'group'}-{index}"


def _style_vars(style: CategoryStyle, index: int | None) -> str:
    """Emit the inline custom properties a tile/section needs: gradient and stagger index.

    ``--i`` drives ``animation-delay: calc(var(--i) * 45ms)`` so a row of tiles or cards
    enters one after another; it is omitted for the category section itself, which enters as
    a whole. The accents are module constants (a ``#rrggbb`` literal or a ``var(--…)``
    reference), never anything plugin-derived.
    """
    parts = [] if index is None else [f"--i:{index}"]
    parts.append(f"--cat-from:{html.escape(style.accent_from)}")
    parts.append(f"--cat-to:{html.escape(style.accent_to)}")
    return f'style="{";".join(parts)}"'


def _icon_html(style: CategoryStyle) -> str:
    """Render a category's icon.

    ``style.icon`` is escaped like everything else, even though today it is a module constant
    of SVG path data whose characters escaping does not touch. Leaving one unescaped hole
    because the value "is trusted" makes the safety a property of who edits the table rather
    than of the code; the day a style becomes configurable, or is read from a plugin, the hole
    is an attribute breakout and nothing here would have changed to warn about it.

    The icon is decorative — the category name sits right beside it — so it is hidden from
    assistive tech rather than labelled. It gets no ``title=``, which would also resurrect the
    raw browser tooltip the (i) popover was built to replace.
    """
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'focusable="false"><path d="{html.escape(style.icon)}"/></svg>'
    )


#: Omnia's own actions, which belong to no plugin and therefore to no category. They live on the
#: HEADER ROW rather than among the tiles: the grid is the list of features you turn on, and a
#: tile sitting in it that opens a window instead implies Sync is one of them. Beside the title
#: it reads as what it is — something the whole add-on does.
#:
#: Each keeps the :class:`CategoryStyle` it had as a tile, gradient and all. The icon is the
#: thing people recognise the button by, and a header button is not a reason to redraw it flat.
HEADER_ACTIONS: tuple[tuple[str, str, CategoryStyle], ...] = (
    (
        "sync",
        "Sync",
        CategoryStyle(
            icon=(
                "M4 12a8 8 0 0 1 13.7-5.7L20 8.5M20 4v4.5H15.5"
                "M20 12a8 8 0 0 1-13.7 5.7L4 15.5M4 20v-4.5h4.5"
            ),
            blurb="Copy decks and settings from your other computer.",
            accent_from="#0ea5e9",
            accent_to="#6366f1",
        ),
    ),
)


def _header_actions_html() -> str:
    """The buttons beside the title."""
    return (
        '<div class="omnia-header-actions">'
        + "".join(
            _header_action_html(op, label, style) for op, label, style in HEADER_ACTIONS
        )
        + "</div>"
    )


def _header_action_html(op: str, label: str, style: CategoryStyle) -> str:
    """One header button: its icon, its name, and the op it sends.

    Marked ``data-action`` rather than ``data-category`` because the JS pairs a category handle
    with the section carrying the same one — an action wearing a category attribute would look
    for a view that does not exist and open nothing at all.

    No ``title`` attribute: the page bans raw browser tooltips (unstyled, slow and untouchable),
    and this button carries its name in the open anyway.
    """
    return (
        f'<button type="button" class="omnia-action" data-action="{op}" '
        f"{_style_vars(style, None)}>"
        # The fill sits BEHIND the label and is driven by one custom property. A separate
        # progress bar would need somewhere to live on a header row that has no room for one,
        # and a button that fills up is readable from across the desk.
        '<span class="omnia-action-fill" aria-hidden="true"></span>'
        f'<span class="omnia-action-icon" aria-hidden="true">{_icon_html(style)}</span>'
        f"<span>{html.escape(label)}</span>"
        '<span class="omnia-action-tip" role="status"></span>'
        "</button>"
    )


def _landing_html(categories: list[CategoryModel]) -> str:
    """Render the landing view: one tile per category."""
    tiles = [_tile_html(category, index) for index, category in enumerate(categories)]
    body = (
        f'<div class="omnia-tiles">{"".join(tiles)}</div>'
        if tiles
        else '<div class="omnia-empty">No feature plugins are installed.</div>'
    )
    return f'<section id="omnia-landing" class="omnia-landing omnia-enter">{body}</section>'


def _tile_html(category: CategoryModel, index: int) -> str:
    """Render one landing tile — a real ``<button>``, so Enter/Space open it for free."""
    on = " omnia-on" if category.on_count else ""
    return (
        f'<button type="button" class="omnia-tile{on}" '
        f'data-category="{category.key}" data-total="{category.total}" '
        f"{_style_vars(category.style, index)}>"
        f'<span class="omnia-tile-icon" aria-hidden="true">{_icon_html(category.style)}</span>'
        f'<span class="omnia-tile-name">{html.escape(category.name)}</span>'
        f'<span class="omnia-tile-blurb">{html.escape(category.style.blurb)}</span>'
        # The sentence is written HERE and only here; settings.js rewrites the number inside
        # the <b> after a toggle, so the two layers cannot drift into different wordings.
        f'<span class="omnia-tile-count">'
        f'<b class="omnia-tile-on">{category.on_count}</b> of {category.total} on</span>'
        "</button>"
    )


def _category_view_html(category: CategoryModel) -> str:
    """Render one category's detail view, hidden until its tile is clicked."""
    cards = "\n".join(
        _card_html(card, index) for index, card in enumerate(category.cards)
    )
    name = html.escape(category.name)
    return (
        f'<section class="omnia-category" data-category="{category.key}" '
        f'aria-label="{name}" {_style_vars(category.style, None)} hidden>'
        '<div class="omnia-cat-head">'
        '<button type="button" class="omnia-back">'
        '<span class="omnia-back-arrow" aria-hidden="true">←</span>Back</button>'
        '<div class="omnia-cat-heading">'
        # Focus lands here when the view opens (see settings.js): a heading names the place
        # you just arrived at, where the section would have been announced as a bare region.
        '<h2 class="omnia-cat-name" tabindex="-1">'
        f'<span class="omnia-cat-icon" aria-hidden="true">{_icon_html(category.style)}</span>'
        f"{name}</h2>"
        f'<div class="omnia-cat-blurb">{html.escape(category.style.blurb)}</div>'
        "</div>"
        "</div>"
        f'<div class="omnia-cards">{cards}</div>'
        "</section>"
    )


def _tip_html(tooltip: str, description: str) -> str:
    """Render the styled (i) help popover for a card, or "" when there's no extended help.

    Only shown when the plugin declares a ``tooltip`` that adds something beyond the inline
    ``description`` (avoids a redundant popover that just repeats the visible text). The body
    keeps the author's line breaks (``\\n`` → ``<br>``) so multi-line/bulleted help reads as
    written instead of one wrapped paragraph.
    """
    extra = tooltip.strip()
    if not extra or extra == description.strip():
        return ""
    body = "<br>".join(html.escape(line) for line in extra.split("\n"))
    return (
        '<span class="omnia-info" tabindex="0" aria-label="More about this feature">'
        '<span class="omnia-info-icon">i</span>'
        f'<span class="omnia-tip" role="tooltip">{body}</span>'
        "</span>"
    )


def _card_html(card: PluginCardModel, index: int) -> str:
    checked = " checked" if card.enabled else ""
    failed = " omnia-failed" if (card.enabled and not card.active) else ""
    configure = (
        f'<button class="omnia-configure" data-id="{html.escape(card.id)}">Configure…</button>'
        if card.configurable
        else ""
    )
    return (
        f'<div class="omnia-card{failed}" data-id="{html.escape(card.id)}" '
        f'style="--i:{index}">'
        '<div class="omnia-card-text">'
        '<div class="omnia-card-title">'
        f"{html.escape(card.name or card.id)}"
        f"{_tip_html(card.tooltip, card.description)}"
        "</div>"
        f'<div class="omnia-card-desc">{html.escape(card.description)}</div>'
        f'<div class="omnia-card-status">{html.escape(status_text(enabled=card.enabled, active=card.active))}</div>'
        "</div>"
        # Where a background job says how far it has got. Built empty for every card and filled
        # by JS from whatever the plugin publishes under "<id>.progress" — a card whose plugin
        # has no such service never shows it, so nothing here knows which plugins have jobs.
        '<div class="omnia-card-job" data-progress="none">'
        '<div class="omnia-card-job-fill"></div>'
        '<span class="omnia-card-job-text"></span>'
        # No native `title=`: this page uses its own popovers throughout, and a test pins that
        # (a native tooltip looks nothing like the rest and cannot be styled). "Stop" beside a
        # running count needs no explaining anyway.
        f'<button class="omnia-card-job-stop" data-stop="{html.escape(card.id)}">'
        "Stop</button>"
        "</div>"
        '<div class="omnia-card-actions">'
        f"{configure}"
        '<label class="omnia-switch">'
        f'<input type="checkbox" data-id="{html.escape(card.id)}"{checked}>'
        '<span class="omnia-slider"></span>'
        "</label>"
        "</div>"
        "</div>"
    )
