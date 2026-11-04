"""Cultivation feature: a daily level for your English vocabulary and your DSA practice.

Each domain gets a score that rises with real ability and falls with neglect, and a level that
simply follows the score — named twice, as a cultivation realm (Luyện Khí … Chân Tiên) and on the
scale people already know: CEFR (A1 … C2) for English, the Codeforces tiers for DSA. Only what
happens from the start day on counts; "Start over" moves the start day to today.

* **English** — computed from the open collection, strictly read-only: FSRS recall probability
  per card, weighted by how strong the card's evidence is (typed recall > recall > recognition),
  best card per word, summed. Unit: word-equivalents, which is what the CEFR bands are in.
* **DSA** — an Elo rating replayed from dsa-journey's attempts. The repo's ``/commit-progress``
  command runs ``scripts/cultivation_feed.py`` as its last step, which writes the feed this plugin
  reads; the window notices the file change by itself.

Layout:

* pure logic — :mod:`.realms` (ladders), :mod:`.memory`, :mod:`.vocab`, :mod:`.dsa`,
  :mod:`.streak` (unit-tested headless);
* :mod:`.store` — the plugin's own SQLite file in ``user_files/cultivation/``;
* :mod:`.reader` + :mod:`.service` — the daily run (collection passed in);
* this module — the only part that touches Anki: the Tools menu entry, the start day, the
  refresh triggers and the window (:mod:`omnia.gui.cultivation`).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import date
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.cultivation.config import CultivationSettings
from omnia.plugins.cultivation.service import CultivationService
from omnia.plugins.cultivation.store import CultivationStore

logger = get_logger("cultivation")

_MENU_LABEL = "Cultivation"
_STATE_FILE = ("cultivation", "state.db")
# Leaving these screens means reviews may have changed the numbers.
_REVIEW_STATES = ("review", "overview")


@register("cultivation")
class CultivationPlugin(FeaturePlugin):
    """Tools → Cultivation: realms, power and trials for vocabulary and DSA."""

    name = "Cultivation"
    description = "Your English (CEFR) and DSA (Elo) level, cultivation-novel style."
    tooltip = (
        "A daily level for two learning streams: English vocabulary from your Anki collection "
        "(read-only), shown as CEFR, and DSA from the dsa-journey repo, shown as an Elo rating. "
        "Both rise with real practice and fall with neglect, each named as a cultivation realm. "
        "Open it from Tools → Cultivation."
    )
    group = "Progress"
    order = 70
    config_model = CultivationSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._settings: Optional[CultivationSettings] = None
        self._service: Optional[CultivationService] = None
        self._action: Any = None
        self._window: Any = None
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []

    # -- lifecycle -------------------------------------------------------------------------

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        settings = ctx.settings
        self._settings = (
            settings
            if isinstance(settings, CultivationSettings)
            else CultivationSettings()
        )
        self._service = None
        if self._settings.start() is None:
            # The first enable is day one: nothing before it counts until the user says so.
            self._write({"start_date": date.today().isoformat()})
        self._action = anki_compat.add_tools_menu_action(
            _MENU_LABEL, lambda _checked: self.open_window()
        )
        anki_compat.subscribe_hook("state_did_change", self._on_state_change)
        anki_compat.subscribe_hook("sync_did_finish", self._on_sync)
        # Record today even if the window is never opened (the history chart needs the days).
        anki_compat.single_shot(3000, self.refresh_anki)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook("state_did_change", self._on_state_change)
        anki_compat.unsubscribe_hook("sync_did_finish", self._on_sync)
        anki_compat.remove_tools_menu_action(self._action)
        self._action = None
        if self._window is not None:
            with contextlib.suppress(RuntimeError):  # already deleted by Qt
                self._window.close()
        self._window = None
        self._listeners = []
        self._service = None
        self._ctx = None

    # -- the service ----------------------------------------------------------------------

    @property
    def settings(self) -> CultivationSettings:
        return self._settings or CultivationSettings()

    def service(self) -> CultivationService:
        """The service, built on first use (the store lives in ``user_files``)."""
        if self._service is None:
            if self._ctx is None:
                raise RuntimeError("Cultivation is not enabled")
            path = self._ctx.paths.user_files_dir.joinpath(*_STATE_FILE)
            self._service = CultivationService(
                self.settings,
                CultivationStore(path),
                self.settings.start() or date.today(),
            )
        return self._service

    def save_setup(self, values: dict[str, Any]) -> None:
        """Persist the Setup tab's picks and rebuild the service with them.

        Rebuilt in place rather than via ``reload_self``: a reload disables the plugin, and
        disabling closes this very window while the user is still in it.
        """
        self._write(values)

    def start_over(self) -> None:
        """Forget the recorded history and count from today."""
        self.service().reset()
        self._write({"start_date": date.today().isoformat()})

    def _write(self, values: dict[str, Any]) -> None:
        """Merge ``values`` into this plugin's config and rebuild the service with them."""
        if self._ctx is None:
            return
        self._ctx.config.update_section(self.id, values)
        fresh = self._ctx.config.feature_settings(self.id)
        if isinstance(fresh, CultivationSettings):
            self._settings = fresh
        self._service = None

    # -- refresh -------------------------------------------------------------------------

    def add_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        """Called with ``(domain, card)`` after every refresh (the open window redraws)."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, domain: str, card: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(domain, card)
            except Exception:
                logger.exception("cultivation: a refresh listener failed")

    def refresh_anki(self) -> None:
        """Recompute the Anki card off the main thread, then notify listeners."""
        if self._ctx is None:
            return
        mw = anki_compat.main_window()
        if getattr(mw, "col", None) is None:
            return
        service = self.service()

        def work() -> dict[str, Any]:
            return service.refresh_anki(mw.col, time.time())

        def failed(exc: Exception) -> None:
            logger.error("cultivation: Anki refresh failed: %s", exc)
            self._emit("anki", {"domain": "anki", "missing": True, "error": str(exc)})

        anki_compat.run_in_background(
            work, on_success=lambda card: self._emit("anki", card), on_failure=failed
        )

    def refresh_dsa(self) -> dict[str, Any]:
        """Recompute the DSA card (a small file and a replay — fast enough for the main thread)."""
        card = self.service().refresh_dsa(date.today())
        self._emit("dsa", card)
        return card

    def _on_state_change(self, new_state: str, old_state: str) -> None:
        if old_state in _REVIEW_STATES and new_state != old_state:
            self.refresh_anki()

    def _on_sync(self, *_args: Any) -> None:
        self.refresh_anki()

    # -- the window ----------------------------------------------------------------------

    def open_window(self) -> None:
        """Show the Cultivation window (one at a time)."""
        from omnia.gui.cultivation import CultivationWindow

        if self._window is not None:
            try:
                self._window.raise_()
                self._window.activateWindow()
                return
            except RuntimeError:  # closed and deleted
                self._window = None
        self._window = CultivationWindow(self, anki_compat.main_window())
        self._window.finished.connect(self._on_window_closed)
        self._window.show()

    def _on_window_closed(self, *_args: Any) -> None:
        self._window = None
