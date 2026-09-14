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
    PairingAddress,
    PairingError,
    SyncClient,
    SyncError,
    access_code,
    machine_id,
    parse_machine_id,
    parse_passcode,
)
from omnia.gui.sync import session
from omnia.gui.sync.collect import inventory_reader
from omnia.gui.sync.html import PanelState, build_sync_html
from omnia.gui.web_dialog import WebDialog

logger = get_logger("sync")

#: Where the other machine's ID and code are remembered. They name a COMPUTER rather than a
#: collection, so they belong beside this machine's own settings in ``machine.toml`` — the other
#: machine in this pair is not the same one once the collection syncs to a third.
#:
#: The code is remembered as well as the ID because they are a pair: keeping half of it means
#: retyping nine digits on every check, which is the part of this people give up on.
PEER_KEY = "peer_id"
PEER_CODE_KEY = "peer_code"


class SyncDialog(WebDialog):
    """Shows this machine's ID, and asks the other machine what it has."""

    def __init__(self, repo: Any, parent: Any = None) -> None:
        self._repo = repo
        self._settings = MachineSettings(repo)
        self._inventory = inventory_reader(repo)
        # Held so the picker is not garbage-collected the moment this method returns — it is
        # shown, not exec'd, so nothing else on the Python side refers to it.
        self._offer: Any = None
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
            access_code=access_code(identity) if live else "",
            peer_id=self._remembered(PEER_KEY),
            peer_code=self._remembered(PEER_CODE_KEY),
        )

    def _remembered(self, key: str) -> str:
        try:
            return str(self._repo.raw_section("sync").get(key, "") or "")
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
            self._show(self._off())
            return {"ok": True}

        identity = self._settings.identity()
        started = session.start(identity, self._inventory)
        if started:
            self._show(
                PanelState(
                    sharing=True,
                    machine_id=machine_id(identity),
                    access_code=access_code(identity),
                    peer_id=self._state.peer_id,
                    peer_code=self._state.peer_code,
                )
            )
        else:
            # The preference is put back too: leaving it on would restore a socket at the next
            # profile open that could not be opened now, and the panel would keep disagreeing
            # with itself.
            self._settings.set_sharing(False)
            self._show(
                self._off(
                    status=(
                        f"Sharing could not start — port {identity.port} is already in use. "
                        "Close the other copy of Anki, or use a different port."
                    )
                )
            )
        return {"ok": started}

    def _on_regenerate(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Mint a new access code, which stops the old one opening this machine."""
        identity = self._settings.regenerate()
        session.stop()
        live = identity.sharing and session.start(identity, self._inventory)
        self._show(
            PanelState(
                sharing=live,
                machine_id=machine_id(identity) if live else "",
                access_code=access_code(identity) if live else "",
                peer_id=self._state.peer_id,
                peer_code=self._state.peer_code,
                status=(
                    "This computer has a new access code. The old one no longer opens it — "
                    "the ID has not changed."
                ),
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
        typed_code = str(data.get("code", ""))
        try:
            host, port = parse_machine_id(typed)
            address = PairingAddress(host, port, parse_passcode(typed_code))
        except PairingError as exc:
            # Reported without dialling anything: a mistyped number is answered in the moment
            # the user is still looking at the box they typed it into, rather than after the
            # full connection timeout with "could not connect".
            self._show(self._with_status(typed, typed_code, str(exc)))
            return

        self._repo.update_section("sync", {PEER_KEY: typed, PEER_CODE_KEY: typed_code})
        client = SyncClient(address)
        anki_compat.run_in_background(
            client.inventory,
            on_success=lambda inventory: self._show_offer(typed, typed_code, inventory),
            on_failure=lambda exc: self._show_failure(typed, typed_code, exc),
            label="Omnia: asking the other computer…",
        )

    # --- callbacks ---------------------------------------------------------------------
    def _show_offer(self, peer_id: str, peer_code: str, inventory: Any) -> None:
        """Say it connected, then open what the other machine has in its own window."""
        self._show(
            PanelState(
                sharing=self._state.sharing,
                machine_id=self._state.machine_id,
                access_code=self._state.access_code,
                peer_id=peer_id,
                peer_code=peer_code,
                status=f"Connected to {inventory.machine or 'the other computer'}.",
                connected=True,
            )
        )
        # Deferred, like every other webview this add-on opens from inside a callback: a nested
        # AnkiWebView built synchronously here loads with full content and never composites,
        # which reads as a blank window. A 0ms timer lets this return first.
        from aqt.qt import QTimer

        QTimer.singleShot(0, lambda: self._open_offer(inventory))

    def _open_offer(self, inventory: Any) -> None:
        from omnia.gui.sync.offer import open_offer_dialog

        self._offer = open_offer_dialog(inventory, self)

    def _show_failure(self, peer_id: str, peer_code: str, exc: BaseException) -> None:
        # A SyncError already carries the sentence that names its own fix; anything else is a bug
        # HERE, and the user still gets words rather than a dialog full of traceback.
        if isinstance(exc, SyncError):
            message = str(exc)
        else:
            logger.error("sync: asking the other machine failed", exc_info=exc)
            message = "Something went wrong on this computer — see the Omnia log."
        self._show(self._with_status(peer_id, peer_code, message))

    def _with_status(self, peer_id: str, peer_code: str, status: str) -> PanelState:
        """This machine's half unchanged, the other machine's half replaced by ``status``."""
        return PanelState(
            sharing=self._state.sharing,
            machine_id=self._state.machine_id,
            access_code=self._state.access_code,
            peer_id=peer_id,
            peer_code=peer_code,
            status=status,
        )

    def _off(self, *, status: str = "") -> PanelState:
        """Sharing off, with whatever the user typed about the OTHER machine left alone."""
        return PanelState(
            sharing=False,
            peer_id=self._state.peer_id,
            peer_code=self._state.peer_code,
            status=status,
        )


def open_sync_dialog(repo: Any, parent: Any = None) -> None:
    """Open the panel. The one entry point the settings page's action tile reaches."""
    SyncDialog(repo, parent).exec()
