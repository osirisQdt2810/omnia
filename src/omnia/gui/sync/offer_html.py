"""The picker: what the other machine has, and what to take from it.

Its own page, and its own window, rather than more rows under the pairing panel. Those are two
different jobs — one is set up once per pair of machines and then never looked at again, the
other is the thing you come back to — and a panel that does both makes the ID and the access code
scroll away exactly when somebody is trying to read them out.

How the tree behaves is the whole design:

* **Collapsed to the top level.** A real collection has hundreds of decks; a list that opens with
  all of them is one nobody reads. An arrow on any deck that has sub-decks opens ONE level.
* **Clicking the name picks the deck and everything under it**, and clicking again unpicks the
  same. That is what "sync this deck" means, and making somebody tick forty children by hand is
  how a picker gets abandoned halfway.
* **A parent whose children were picked one at a time renders partial**, never picked — drawing
  it as picked would promise to bring a parent nobody chose.

Pure string building, no ``aqt`` — the page unit-tests headless. The selection rules live in
:mod:`omnia.core.sync.tree`, which has no HTML in it.
"""

from __future__ import annotations

import html
from typing import Any

from omnia.core.sync.tree import DeckRow, DeckTree
from omnia.gui.assets import read_asset


def build_offer_html(inventory: Any, *, dark: bool) -> str:
    """Build the complete picker page for ``inventory``."""
    tree = DeckTree.from_entries(inventory.decks)
    return read_asset(__file__, "web", "offer.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "web", "offer.css"),
        machine=html.escape(inventory.machine or "the other computer"),
        decks=_tree_html(tree),
        extras=_extras_html(inventory),
        js=read_asset(__file__, "web", "offer.js"),
    )


def _tree_html(tree: DeckTree) -> str:
    """Every deck as a row, collapsed to the top level."""
    rows = tree.rows()
    if not rows:
        return '<p class="offer-note">There are no decks on that computer.</p>'
    return f'<ul class="offer-tree">{"".join(_row_html(row) for row in rows)}</ul>'


def _row_html(row: DeckRow) -> str:
    """One deck.

    Everything the page needs to run the tree is on the element: its full name, its depth, and
    whether it starts hidden. The JS reads structure out of the NAMES (a sub-deck's name starts
    with its parent's, plus ``::``) rather than keeping a parallel model that can disagree with
    what is on screen.
    """
    node = row.node
    arrow = (
        '<button type="button" class="offer-arrow" aria-label="Show sub-decks">'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" '
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg></button>'
        if row.expandable
        else '<span class="offer-arrow offer-arrow-none" aria-hidden="true"></span>'
    )
    return (
        f'<li class="offer-row{"" if row.depth == 0 else " offer-hidden"}" '
        f'data-deck="{html.escape(node.name, quote=True)}" data-depth="{row.depth}" '
        f'data-cards="{node.cards}" style="--depth:{row.depth}">'
        f"{arrow}"
        f'<button type="button" class="offer-name">{html.escape(row.label)}</button>'
        f'<span class="offer-cards">{_count_html(row)}</span>'
        "</li>"
    )


def _count_html(row: DeckRow) -> str:
    """The one number on the right of a row, and what it counts.

    A parent is picked for what is UNDER it, so that is the number it shows; its own cards are
    a footnote and appear only when there are any. Showing both unconditionally put
    "75,760 in all" beside a bare "0" on every parent in the tree, which reads as two
    unrelated figures and made the column impossible to scan.
    """
    node = row.node
    if not row.expandable:
        return f"{node.cards:,}"
    total = f'{node.total_cards:,} <span class="offer-total">in all</span>'
    if not node.cards:
        return total
    return f'{total} <span class="offer-total">\u00b7 {node.cards:,} here</span>'


def _extras_html(inventory: Any) -> str:
    """Note types and settings, as two sections of chips you click.

    Note types are not picked the way decks are. They light up because a chosen deck needs them
    — a different colour, because it is a different thing from choosing something — and clicking
    one DROPS it: the deck still comes, its notes of that type do not. A deck holding two note
    types where only one is worth carrying is the case this exists for.
    """
    return (
        '<div class="offer-extras">'
        + _section_html(
            "note-types",
            "Note types",
            "They light up when a deck needs them. Click one to leave it behind — the deck "
            "still comes, its notes of that type do not.",
            [
                _chip_html("note-type", entry.name, _notes_label(entry))
                for entry in getattr(inventory, "note_types", ())
            ],
            empty="That computer has no note types.",
        )
        + _section_html(
            "config",
            "Settings",
            "Omnia's own settings for these features. API keys never travel.",
            [
                _chip_html("feature", name, "")
                for name in getattr(inventory.config, "features", ())
            ],
            empty="Nothing is configured over there.",
        )
        + "</div>"
    )


def _section_html(
    handle: str, title: str, note: str, chips: list[str], *, empty: str
) -> str:
    body = (
        f'<div class="offer-chips">{"".join(chips)}</div>'
        if chips
        else f'<p class="offer-note">{html.escape(empty)}</p>'
    )
    return (
        f'<section class="offer-extra" data-extra="{handle}">'
        f"<h3>{html.escape(title)}</h3>"
        f'<p class="offer-note">{html.escape(note)}</p>'
        f"{body}"
        "</section>"
    )


def _chip_html(kind: str, name: str, detail: str) -> str:
    """One clickable chip. Its state is a class the page is TOLD, never one it works out."""
    suffix = (
        f'<span class="offer-chip-detail">{html.escape(detail)}</span>'
        if detail
        else ""
    )
    return (
        f'<button type="button" class="offer-chip offer-idle" data-kind="{kind}" '
        f'data-name="{html.escape(name, quote=True)}">'
        f'<span class="offer-chip-name">{html.escape(name)}</span>{suffix}'
        "</button>"
    )


def _notes_label(entry: Any) -> str:
    """How many notes use a note type — the number that says whether it matters."""
    count = int(getattr(entry, "notes", 0) or 0)
    if not count:
        return ""
    return f"{count:,}"
