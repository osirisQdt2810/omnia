"""The picker window: what the other machine has, and what to take.

Separate from :class:`~omnia.gui.sync.dialog.SyncDialog` because they are separate jobs. Pairing
is done once per pair of machines and then never looked at again; picking is the thing somebody
comes back to. Sharing one window would mean the ID and the access code scrolling away exactly
when the other machine's owner is trying to read them out.

Thin Qt glue. The tree's behaviour is in the page, the selection rules are in
:mod:`omnia.core.sync.tree`, and the markup is in :mod:`omnia.gui.sync.offer_html`.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.sync.selection import OfferSelection
from omnia.gui.sync.offer_html import build_offer_html
from omnia.gui.web_dialog import WebDialog

logger = get_logger("sync")


class OfferDialog(WebDialog):
    """Shows one machine's decks, note types and settings, and takes a selection."""

    def __init__(self, inventory: Any, parent: Any = None) -> None:
        self._inventory = inventory
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

    def _on_pull(self, data: dict[str, Any]) -> dict[str, Any]:
        """Take what was picked.

        The copying itself is the next slice. Until it exists this says so plainly rather than
        appearing to work: a button that looks like it copied decks and did not is worse than one
        that admits it is not finished.
        """
        logger.info(
            "sync: asked to pull %d deck(s), %d note type(s), %d setting(s)",
            len(self._selection.decks),
            len(self._selection.note_types),
            len(self._selection.features),
        )
        return {
            "ok": False,
            "message": (
                "Copying is not switched on yet — this build can show you what the other "
                "computer has, and not yet bring it over."
            ),
        }


def open_offer_dialog(inventory: Any, parent: Any = None) -> Optional[OfferDialog]:
    """Open the picker for ``inventory``."""
    dialog = OfferDialog(inventory, parent)
    dialog.show()
    return dialog
