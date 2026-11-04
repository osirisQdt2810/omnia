"""The Cultivation window: a :class:`WebDialog` whose page draws both domains' status cards.

Python pushes each card as it is computed (``window.cultivation.update``); the page asks for
work through ``pycmd`` ops. The Anki card is computed off the main thread by the plugin, the DSA
card on the main thread (a small JSON file and a replay), and the page polls for a changed feed so
a ``/commit-progress`` run in dsa-journey shows up without touching the window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.logging import get_logger
from omnia.gui.cultivation.html import build_cultivation_html
from omnia.gui.web_dialog import WebDialog

if TYPE_CHECKING:
    from omnia.plugins.cultivation import CultivationPlugin

logger = get_logger("cultivation")


class CultivationWindow(WebDialog):
    """Tools → Cultivation."""

    def __init__(self, plugin: CultivationPlugin, parent: Any = None) -> None:
        from aqt.theme import theme_manager

        self._plugin = plugin
        self._feed_mtime: Optional[float] = None
        super().__init__(
            parent,
            title="Cultivation",
            html=build_cultivation_html(dark=theme_manager.night_mode),
            handlers={
                "refresh": self._refresh,
                "poll": self._poll,
                "save_setup": self._save_setup,
                "start_over": self._start_over,
            },
            width=900,
            height=720,
        )
        self.resize(980, 780)
        plugin.add_listener(self._push)

    def done(self, result: int) -> None:
        self._plugin.remove_listener(self._push)
        super().done(result)

    def closeEvent(self, evt: Any) -> None:  # noqa: N802 (Qt override name)
        self._plugin.remove_listener(self._push)
        super().closeEvent(evt)

    # -- pushing cards to the page --------------------------------------------------------

    def _push(self, domain: str, card: dict[str, Any]) -> None:
        self.eval_js(
            "window.cultivation && window.cultivation.update("
            f"{json.dumps(domain)}, {json.dumps(card, default=str)})"
        )

    # -- ops ------------------------------------------------------------------------------

    def _feed_path(self) -> Path:
        return self._plugin.settings.feed_path()

    def _current_mtime(self) -> Optional[float]:
        try:
            return self._feed_path().stat().st_mtime
        except OSError:
            return None

    def _refresh(self, _data: dict[str, Any]) -> bool:
        self._plugin.refresh_anki()
        self._feed_mtime = self._current_mtime()
        self._plugin.refresh_dsa()
        return True

    def _poll(self, _data: dict[str, Any]) -> bool:
        """Recompute DSA only when the feed file changed since the last look."""
        mtime = self._current_mtime()
        if mtime != self._feed_mtime:
            self._feed_mtime = mtime
            self._plugin.refresh_dsa()
            return True
        return False

    def _save_setup(self, data: dict[str, Any]) -> bool:
        values = {
            "decks": [str(d) for d in data.get("decks", [])],
            "word_fields": {
                str(k): str(v) for k, v in data.get("word_fields", {}).items()
            },
            "modality": {str(k): str(v) for k, v in data.get("modality", {}).items()},
        }
        self._plugin.save_setup(values)
        self._plugin.refresh_anki()
        self._plugin.refresh_dsa()
        return True

    def _start_over(self, _data: dict[str, Any]) -> bool:
        """Forget the history and count from today, then redraw both cards."""
        self._plugin.start_over()
        self._plugin.refresh_anki()
        self._plugin.refresh_dsa()
        return True
