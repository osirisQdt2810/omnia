"""The Sync panel's markup: two halves, one per machine, and nothing about networking.

The user's model is two steps and the page is built to show exactly those:

* **This machine** — a switch, the ID to read out, and the access code beneath it. Two numbers,
  the way remote-desktop tools do it, and for the same reason: without a rendezvous server the ID
  has to BE the address, which uses up every digit it has (:mod:`omnia.core.sync.pairing`).
* **The other machine** — a box for its ID, a box for its code, a button that says whether it
  worked, and once it has, what that machine holds.

Every state the panel can be in gets words rather than a spinner that stops: not ready, sharing
off, sharing on, checking, connected, and each way a connection fails. That is the whole design
— a sync that cannot start is the normal case while someone sets this up, and "could not
connect" is the answer that leaves them with nothing to try.

Pure string building, no ``aqt`` — so the page unit-tests headless.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from omnia.gui.assets import read_asset


@dataclass(frozen=True)
class DeckRow:
    """One deck offered by the other machine, ready to render."""

    name: str
    cards: int
    depth: int = 0


@dataclass(frozen=True)
class PanelState:
    """Everything the page needs to draw itself.

    Attributes:
        sharing: Whether this machine currently answers.
        machine_id: The number to show for this machine, or "" when nothing could dial it.
        access_code: The code to show beneath it.
        peer_id: The last ID typed for the other machine, remembered between openings.
        peer_code: The last code typed for it. Remembered too — it is the pair that opens a
            machine, and remembering half of it means retyping the other half every time.
        status: A sentence about the other machine — the result of the last check, or "".
        connected: Whether ``status`` is a success rather than a failure.
        decks: What the other machine holds, once it has been asked.
        note_types: Its note types, by name.
        features: The configured features it holds.
    """

    sharing: bool = False
    machine_id: str = ""
    access_code: str = ""
    peer_id: str = ""
    peer_code: str = ""
    status: str = ""
    connected: bool = False
    decks: tuple[DeckRow, ...] = ()
    note_types: tuple[str, ...] = ()
    features: tuple[str, ...] = field(default_factory=tuple)


def build_sync_html(state: PanelState, *, dark: bool) -> str:
    """Build the complete Sync page."""
    return read_asset(__file__, "web", "sync.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "web", "sync.css"),
        this_machine=_this_machine_html(state),
        other_machine=_other_machine_html(state),
        js=read_asset(__file__, "web", "sync.js"),
    )


def _this_machine_html(state: PanelState) -> str:
    """The half about this computer: the switch, and the ID to read out."""
    checked = " checked" if state.sharing else ""
    if not state.sharing:
        # Not "no ID": the ID exists, it is simply not answering. Saying which of the two is the
        # case is the difference between "turn this on" and "something is wrong here".
        body = (
            '<p class="sync-note">Turn this on when you want to copy from this computer to '
            "another one. It stays off until you do.</p>"
        )
    elif not state.machine_id:
        body = (
            '<p class="sync-warn">This computer has no address another one could reach right '
            "now. Connect it to a network, then turn sharing off and on again.</p>"
        )
    else:
        body = (
            '<p class="sync-note">Type both of these into the other computer.</p>'
            '<div class="sync-pair">'
            f'{_number_html("ID", "sync-id", state.machine_id)}'
            f'{_number_html("Access code", "sync-code", state.access_code)}'
            "</div>"
            '<div class="sync-row">'
            '<button class="sync-btn sync-btn-quiet" id="sync-regen">New code</button>'
            '<span class="sync-hint">The ID stays. A new code stops the old one opening '
            "this computer.</span>"
            "</div>"
        )
    return (
        '<section class="sync-card">'
        '<div class="sync-head"><h2>This computer</h2>'
        '<label class="sync-switch"><input type="checkbox" id="sync-sharing"'
        f'{checked}><span class="sync-slider"></span></label></div>'
        f"{body}"
        "</section>"
    )


def _number_html(label: str, handle: str, value: str) -> str:
    """One of the two numbers, with its label ABOVE it rather than beside it.

    Beside it, the label competes for the row and a nine-digit code wraps onto a second line —
    which is the one thing a number somebody has to transcribe must not do.
    """
    return (
        '<div class="sync-field"><div class="sync-field-body">'
        f'<span class="sync-label">{html.escape(label)}</span>'
        f'<span class="sync-number" id="{handle}">{html.escape(value)}</span></div>'
        f'<button class="sync-btn sync-btn-small" data-copy="{handle}">Copy</button></div>'
    )


def _other_machine_html(state: PanelState) -> str:
    """The half about the other computer: paste its ID, check it, then see what it has."""
    status = ""
    if state.status:
        kind = "sync-ok" if state.connected else "sync-warn"
        status = f'<p class="{kind}" id="sync-status">{html.escape(state.status)}</p>'
    return (
        '<section class="sync-card">'
        "<h2>The other computer</h2>"
        '<p class="sync-note">Type the two numbers it shows.</p>'
        '<div class="sync-row">'
        f'<input class="sync-input" id="sync-peer" value="{html.escape(state.peer_id)}" '
        'placeholder="its ID" inputmode="numeric" spellcheck="false">'
        f'<input class="sync-input sync-input-short" id="sync-peer-code" '
        f'value="{html.escape(state.peer_code)}" placeholder="its access code" '
        'inputmode="numeric" spellcheck="false">'
        '<button class="sync-btn" id="sync-check">Check</button>'
        "</div>"
        f"{status}"
        f"{_offer_html(state)}"
        "</section>"
    )


def _offer_html(state: PanelState) -> str:
    """What the other machine holds, or nothing at all before it has been asked."""
    if not state.connected:
        return ""
    decks = "".join(
        '<li class="sync-deck" style="--depth:{depth}">'
        '<span class="sync-deck-name">{name}</span>'
        '<span class="sync-deck-cards">{cards}</span></li>'.format(
            depth=row.depth,
            name=html.escape(row.name.rsplit("::", 1)[-1]),
            cards=f"{row.cards:,}" if row.cards else "—",
        )
        for row in state.decks
    )
    summary = ", ".join(
        part
        for part in (
            _count(len(state.note_types), "note type"),
            _count(len(state.features), "configured feature"),
        )
        if part
    )
    # Built outside the f-string: a backslash inside one is a SyntaxError before Python 3.12,
    # and Anki's minimum is 3.10 — the kind of break that only shows up on the oldest job in the
    # matrix, as a collection error rather than a failing assertion.
    empty = '<li class="sync-note">No decks over there.</li>'
    return (
        '<div class="sync-offer">'
        f'<ul class="sync-decks">{decks or empty}</ul>'
        f'<p class="sync-note">{html.escape(summary)}</p>'
        "</div>"
    )


def _count(number: int, noun: str) -> str:
    """``2, "note type"`` → ``"2 note types"``; nothing at all for zero."""
    if not number:
        return ""
    return f"{number} {noun}{'' if number == 1 else 's'}"
