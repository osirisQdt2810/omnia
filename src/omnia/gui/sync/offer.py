"""The picker window: what the other machine has, and what to take.

Separate from :class:`~omnia.gui.sync.dialog.SyncDialog` because they are separate jobs. Pairing
is done once per pair of machines and then never looked at again; picking is the thing somebody
comes back to. Sharing one window would mean the ID and the access code scrolling away exactly
when the other machine's owner is trying to read them out.

Thin Qt glue. The tree's behaviour is in the page, the selection rules are in
:mod:`omnia.core.sync.tree`, and the markup is in :mod:`omnia.gui.sync.offer_html`.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.sync.package import PackageError, PackageRequest
from omnia.core.sync.progress import DONE, FAILED
from omnia.core.sync.selection import OfferSelection
from omnia.gui.sync.offer_html import build_offer_html
from omnia.gui.web_dialog import WebDialog

logger = get_logger("sync")


class OfferDialog(WebDialog):
    """Shows one machine's decks, note types and settings, and takes a selection."""

    def __init__(
        self, inventory: Any, client: Any = None, repo: Any = None, parent: Any = None
    ) -> None:
        self._inventory = inventory
        self._client = client
        self._repo = repo
        self._timer: Any = None
        self._job: Any = None
        # The selection lives HERE, not in the page. What a click means is a rule with corners
        # in it — unpicking a parent whose child was picked separately, a note type that lights
        # up because a deck needs it — and the page owning a second copy is how it got painted
        # wrong: the script read its own set while still mutating it, and every expandable
        # sibling of a picked deck came out half-selected.
        self._selection = OfferSelection(inventory)
        machine = inventory.machine or "the other computer"
        super().__init__(
            parent,
            title=f"Omnia — Sync from {machine}",
            html=self._render(),
            handlers={
                "pick_deck": self._on_pick_deck,
                "drop_note_type": self._on_drop_note_type,
                "pick_feature": self._on_pick_feature,
                "pull": self._on_pull,
            },
            width=640,
            height=620,
        )

    def _render(self) -> str:
        from aqt.theme import theme_manager

        return build_offer_html(self._inventory, dark=theme_manager.night_mode)

    def _on_pick_deck(self, data: dict[str, Any]) -> dict[str, Any]:
        """Toggle one deck. Answers for the note types too — they follow the decks."""
        return self._answer(self._selection.pick_deck(str(data.get("name", ""))))

    def _on_drop_note_type(self, data: dict[str, Any]) -> dict[str, Any]:
        """Leave one note type behind, or take it back."""
        return self._answer(self._selection.drop_note_type(str(data.get("name", ""))))

    def _on_pick_feature(self, data: dict[str, Any]) -> dict[str, Any]:
        """Toggle one feature's settings."""
        return self._answer(self._selection.pick_feature(str(data.get("name", ""))))

    def _answer(self, changed: dict[str, Any]) -> dict[str, Any]:
        """One shape for every op: what changed, and the tally, so the page never derives either."""
        answer: dict[str, Any] = {"decks": {}, "note_types": {}, "features": {}}
        answer.update(changed)
        answer["tally"] = self._selection.tally()
        return answer

    def _on_pull(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Start copying what was picked.

        Returns at once. The pull outlives this window on purpose — the user can close it and
        carry on studying — so progress is PUSHED in while the window happens to be open, rather
        than the page asking for it.
        """
        from omnia.gui.sync.job import PullRefusedError, start_pull

        try:
            request = PackageRequest(
                decks=tuple(sorted(self._selection.decks)),
                note_types=tuple(sorted(self._selection.note_types)),
                config=tuple(sorted(self._selection.features)),
            )
        except PackageError as exc:
            return {"refused": str(exc)}
        if self._client is None:
            return {
                "refused": "This window is not connected to the other computer any more."
            }

        logger.info(
            "sync: pulling %d deck(s), %d note type(s), %d setting(s)",
            len(request.decks),
            len(request.note_types),
            len(request.config),
        )
        try:
            self._job = start_pull(
                self._client,
                request,
                self._repo,
                machine=self._inventory.machine or "the other computer",
            )
        except PullRefusedError as exc:
            return {"refused": str(exc)}
        self._watch()
        return {"started": True}

    # --- showing a pull that is running ---------------------------------------------------
    def _watch(self) -> None:
        """Repaint the strip a few times a second while a pull is running.

        A timer rather than a callback from the job: the job outlives this window, and a callback
        held by a closed dialog is a call into a deleted webview. Polling means a window that is
        gone simply stops asking.
        """
        from aqt import mw

        if self._timer is not None:
            return
        self._timer = mw.progress.timer(400, self._tick, True, parent=self)
        self._tick()

    def _tick(self) -> None:
        # The job THIS window started, not whatever is current: a pull can finish between two
        # ticks, and a window that looked up "the current pull" would find it gone and never
        # show the outcome it was waiting for.
        job = self._job
        if job is None:
            self._stop_watching()
            return
        progress = job.snapshot()
        finished = progress.phase in (DONE, FAILED)
        self.eval_js(
            "window.omniaSync && window.omniaSync.showProgress("
            + json.dumps(
                {
                    "percent": progress.percent,
                    "summary": job.result if finished else progress.summary(),
                    "finished": finished,
                }
            )
            + ");"
        )
        if finished:
            self._stop_watching()

    def _stop_watching(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            # A timer whose Qt parent has already gone raises rather than no-opping.
            with contextlib.suppress(Exception):
                timer.stop()


def open_offer_dialog(
    inventory: Any, client: Any = None, repo: Any = None, parent: Any = None
) -> Optional[OfferDialog]:
    """Open the picker for ``inventory``."""
    dialog = OfferDialog(inventory, client, repo, parent)
    dialog.show()
    return dialog
