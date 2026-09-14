"""The Sync dialog: thin Qt glue over the sync seam.

Everything that decides anything lives in :mod:`omnia.core.sync` and is pure; this owns the
window and the four ``pycmd`` ops the page sends. The socket's lifetime belongs to
:mod:`omnia.gui.sync.session`, which outlives this window on purpose.

Two things are deliberate about the shape:

* **The page never learns how the machines reach each other.** It sends ops and renders a state;
  addresses, ports and keys are folded into the ID by :mod:`omnia.core.sync.pairing`.
* **Nothing here blocks the Qt thread on the network.** Checking another machine dials a host
  that may be asleep, which costs the full timeout, so it goes through
  :func:`~omnia.core.anki_compat.run_in_background` and the answer is rendered when it lands.
"""

from __future__ import annotations

from typing import Any

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.sync import (
    MachineSettings,
    PairingError,
    SyncClient,
    SyncError,
    machine_id,
    parse_pairing_code,
)
from omnia.gui.sync import session
from omnia.gui.sync.collect import inventory_reader
from omnia.gui.sync.html import DeckRow, PanelState, build_sync_html
from omnia.gui.web_dialog import WebDialog

logger = get_logger("sync")

#: Where the last-used peer ID is remembered. The ID of the machine you pull FROM names a
#: COMPUTER rather than a collection, so it belongs beside the key in ``machine.toml`` — the
#: other machine in this pair is not the same one after the collection syncs to a third.
PEER_KEY = "peer_id"


class SyncDialog(WebDialog):
    """Shows this machine's ID, and asks the other machine what it has."""

    def __init__(self, repo: Any, parent: Any = None) -> None:
        self._repo = repo
        self._settings = MachineSettings(repo)
        self._inventory = inventory_reader(repo)
        self._state = self._initial_state()
        super().__init__(
            parent,
            title="Omnia — Sync",
            html=self._render(),
            handlers={
                "sharing": self._on_sharing,
                "connect": self._on_connect,
                "copy": self._on_copy,
                "regenerate": self._on_regenerate,
            },
            width=620,
            height=560,
        )

    # --- state ------------------------------------------------------------------------
    def _initial_state(self) -> PanelState:
        """What the panel shows on opening: the switch as it really is, not as it was saved.

        ``sharing and session.running()`` rather than the stored flag alone, because they can
        disagree — sharing was left on and the port is now taken by something else. A switch
        drawn from the preference would then be a lie about a socket that is not there.
        """
        identity = self._settings.identity()
        live = identity.sharing and session.running()
        return PanelState(
            sharing=live,
            machine_id=machine_id(identity) if live else "",
            peer_id=self._peer_id(),
        )

    def _peer_id(self) -> str:
        try:
            return str(self._repo.raw_section("sync").get(PEER_KEY, "") or "")
        except Exception:
            return ""

    def _render(self) -> str:
        from aqt.theme import theme_manager

        return build_sync_html(self._state, dark=theme_manager.night_mode)

    def _show(self, state: PanelState) -> None:
        """Adopt ``state`` and rebuild the page in place.

        A whole re-render rather than a patch per op: partial updates would mean every op
        knowing how its answer changes every other part of the page, which is how a panel ends
        up showing a stale ID beside a fresh status.
        """
        self._state = state
        self.set_html(self._render())

    # --- ops --------------------------------------------------------------------------
    def _on_sharing(self, data: dict[str, Any]) -> dict[str, Any]:
        """Open or close this machine's socket, and remember which the user chose."""
        wanted = bool(data.get("on"))
        self._settings.set_sharing(wanted)
        if not wanted:
            session.stop()
            self._show(PanelState(sharing=False, peer_id=self._state.peer_id))
            return {"ok": True}

        identity = self._settings.identity()
        started = session.start(identity, self._inventory)
        if started:
            self._show(
                PanelState(
                    sharing=True,
                    machine_id=machine_id(identity),
                    peer_id=self._state.peer_id,
                )
            )
        else:
            # The preference is put back too: leaving it on would restore a socket at the next
            # profile open that could not be opened now, and the panel would keep disagreeing
            # with itself.
            self._settings.set_sharing(False)
            self._show(
                PanelState(
                    sharing=False,
                    peer_id=self._state.peer_id,
                    status=(
                        f"Sharing could not start — port {identity.port} is already in use. "
                        "Close the other copy of Anki, or use a different port."
                    ),
                )
            )
        return {"ok": started}

    def _on_regenerate(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Mint a new key, which stops every ID handed out before from opening anything."""
        identity = self._settings.regenerate()
        session.stop()
        live = identity.sharing and session.start(identity, self._inventory)
        self._show(
            PanelState(
                sharing=live,
                machine_id=machine_id(identity) if live else "",
                peer_id=self._state.peer_id,
                status="This computer has a new ID. The old one no longer opens it.",
            )
        )
        return {"ok": True}

    def _on_copy(self, data: dict[str, Any]) -> dict[str, Any]:
        """Put the ID on the clipboard — Python owns it, so the button behaves everywhere."""
        from aqt.qt import QApplication

        QApplication.clipboard().setText(str(data.get("text", "")))
        return {"ok": True}

    def _on_connect(self, data: dict[str, Any]) -> None:
        """Ask the other machine what it has, OFF the Qt thread.

        A machine that is asleep costs the full timeout to find out about, and a frozen Anki is
        a worse answer than a slow one. Returns nothing: the page has already disabled its
        button, and the answer arrives as a re-render rather than as this call's return value.
        """
        typed = str(data.get("id", ""))
        try:
            address = parse_pairing_code(typed)
        except PairingError as exc:
            self._show(self._with_status(typed, str(exc)))
            return

        self._repo.update_section("sync", {PEER_KEY: typed})
        client = SyncClient(address)
        anki_compat.run_in_background(
            client.inventory,
            on_success=lambda inventory: self._show_offer(typed, inventory),
            on_failure=lambda exc: self._show_failure(typed, exc),
            label="Omnia: asking the other computer…",
        )

    # --- callbacks ---------------------------------------------------------------------
    def _show_offer(self, peer_id: str, inventory: Any) -> None:
        self._show(
            PanelState(
                sharing=self._state.sharing,
                machine_id=self._state.machine_id,
                peer_id=peer_id,
                status=f"Connected to {inventory.machine or 'the other computer'}.",
                connected=True,
                decks=tuple(
                    DeckRow(name=deck.name, cards=deck.cards, depth=depth)
                    for deck, depth in inventory.deck_tree()
                ),
                note_types=tuple(entry.name for entry in inventory.note_types),
                features=tuple(inventory.config.features),
            )
        )

    def _show_failure(self, peer_id: str, exc: BaseException) -> None:
        # A SyncError already carries the sentence that names its own fix; anything else is a bug
        # HERE, and the user still gets words rather than a dialog full of traceback.
        if isinstance(exc, SyncError):
            message = str(exc)
        else:
            logger.error("sync: asking the other machine failed", exc_info=exc)
            message = "Something went wrong on this computer — see the Omnia log."
        self._show(self._with_status(peer_id, message))

    def _with_status(self, peer_id: str, status: str) -> PanelState:
        """This machine's half unchanged, the other machine's half replaced by ``status``."""
        return PanelState(
            sharing=self._state.sharing,
            machine_id=self._state.machine_id,
            peer_id=peer_id,
            status=status,
        )


def open_sync_dialog(repo: Any, parent: Any = None) -> None:
    """Open the panel. The one entry point the settings page's action tile reaches."""
    SyncDialog(repo, parent).exec()
