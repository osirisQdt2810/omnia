"""The Omnia settings dialog — a grouped, animated webview list of feature plugins.

Built on the reusable :class:`~omnia.gui.web_dialog.WebDialog` seam: the whole UI is HTML/CSS/
JS rendered inside an ``AnkiWebView`` (gradients, animated toggle switches, hover lift, and
tooltips — beyond what raw Qt stylesheets allow). The page is built by the pure
``settings_html`` module; this class is the thin Qt glue that supplies the view-model and
handles the two ``pycmd`` ops (``toggle`` / ``configure``). Only loaded inside Anki.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from aqt.theme import theme_manager

from omnia.core.logging import get_logger
from omnia.core.manager import grouped_plugins
from omnia.gui.config_panel import panel_payload
from omnia.gui.settings_categories import category_order, category_style
from omnia.gui.settings_html import (
    PluginCardModel,
    build_settings_html,
    category_key,
    status_text,
)
from omnia.gui.web_dialog import WebDialog

if TYPE_CHECKING:
    from omnia.core.manager import PluginManager
    from omnia.core.plugin import FeaturePlugin

logger = get_logger()


#: Every ``pycmd`` op the page can send, and the method that answers it. Declared rather than
#: written inline so the page and the router can be checked against each other: the landing grid
#: renders one button per :data:`~omnia.gui.settings_html.HEADER_ACTIONS` entry, and a button
#: whose op has no handler here is an advertised control that silently does nothing —
#: :class:`~omnia.gui.web_dialog.WebDialog` drops an unrouted message without a word. The Sync
#: tile shipped exactly that way, which is why this is a map a test can read.
HANDLERS = {
    "toggle": "_on_toggle",
    "configure": "_on_configure",
    "save-config": "_on_save_config",
    "sync": "_on_sync",
    "stop-job": "_on_stop_job",
}


class SettingsDialog(WebDialog):
    """Lists every feature plugin, grouped, with a live toggle and a Configure button."""

    def __init__(self, manager: PluginManager, parent: Any = None) -> None:
        self._manager = manager
        #: The group names the page was rendered from, filled by :meth:`_render`.
        self._category_names: list[str] = []
        self._sync_timer: Any = None
        #: What the button currently shows, so an unchanged state is not repainted 120 times a
        #: minute into a webview that is also rendering a settings grid.
        self._sync_shown: Any = (None, "")
        #: Same, per plugin card. Keyed by plugin id because the cards update independently and
        #: a single "last state" would repaint all of them whenever any one changed.
        self._jobs_shown: dict[str, Any] = {}
        super().__init__(
            parent,
            title="Omnia — All-in-One Toolkit",
            html=self._render(),
            handlers={op: getattr(self, name) for op, name in HANDLERS.items()},
            width=720,
            height=620,
        )

    def _render(self) -> str:
        grouped = grouped_plugins(self._manager, order=category_order())
        # The names the page is ACTUALLY built from, remembered because the config panel has to
        # name the category to return to and the handles carry a position — see
        # settings_html.category_key for why the configured order will not do.
        self._category_names = [name for name, _ in grouped]
        groups = [
            (name, [self._card(plugin) for plugin in plugins])
            for name, plugins in grouped
        ]
        return build_settings_html(groups, dark=theme_manager.night_mode)

    def _card(self, plugin: FeaturePlugin) -> PluginCardModel:
        return PluginCardModel(
            id=plugin.id,
            name=plugin.name or plugin.id,
            description=plugin.description,
            # Only the extended help (empty when a plugin sets no tooltip) — the card shows an
            # (i) popover for it; the plain description is already rendered inline.
            tooltip=plugin.tooltip,
            enabled=self._manager.config.is_enabled(plugin.id),
            active=self._manager.is_active(plugin.id),
            configurable=bool(
                plugin.config_schema() or plugin.has_custom_config_dialog()
            ),
        )

    # --- pycmd handlers -------------------------------------------------------------
    def _on_toggle(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply a switch toggle; return the resulting active state for the JS to reflect."""
        plugin_id = str(data.get("id", ""))
        enabled = bool(data.get("enabled"))
        try:
            active = self._manager.set_enabled(plugin_id, enabled)
        except KeyError:
            return {"active": False, "status": "unknown plugin"}
        return {
            "active": active,
            "status": status_text(enabled=enabled, active=active),
        }

    def _on_configure(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Answer a Configure press.

        Two different things wear one button. A plugin whose settings are declared
        :class:`~omnia.core.plugin.ConfigField` values is answered with a payload the PAGE
        renders, in place, as a third view — no second window, and the form wears the same
        gradients and animations as everything around it.

        A plugin that owns a BESPOKE dialog (Smart Notes, Note Maintenance) still opens one, and
        that half must be DEFERRED off this pycmd/bridge callback: opening a webview dialog
        synchronously from inside the settings webview's bridge callback leaves the nested
        AnkiWebView loaded but never composited — the "blank Smart Notes dialog". A 0ms timer
        lets this callback return so the child opens on a clean event-loop turn and paints.

        Returns:
            The panel payload, or ``None`` when a bespoke dialog is opening instead.
        """
        plugin = self._plugin(str(data.get("id", "")))
        if plugin is None:
            return None
        if plugin.has_custom_config_dialog():
            from aqt.qt import QTimer

            QTimer.singleShot(0, lambda p=plugin: self._open_custom_dialog(p))
            return None
        return self._panel_payload(plugin)

    def _plugin(self, plugin_id: str) -> Optional[FeaturePlugin]:
        return next((p for p in self._manager.plugins() if p.id == plugin_id), None)

    def _panel_payload(self, plugin: FeaturePlugin) -> dict[str, Any]:
        """What the page needs to draw this plugin's settings."""
        try:
            settings = self._manager.config.feature_settings(plugin.id)
            values = settings.dict() if settings is not None else {}
        except Exception:
            # An unparseable section — written by an older build, hand-edited, or carried in by
            # sync — makes `feature_settings` raise for the WHOLE section. Refusing to open
            # here is what turns that into a dead button with no message and no way back. The
            # stored values are shown raw instead, so the offending field can be corrected in
            # the panel that will then refuse to save it wrong again.
            logger.exception("settings: %s has an unreadable section", plugin.id)
            values = self._manager.config.raw_section(plugin.id)
        style = category_style(plugin.group)
        return panel_payload(
            plugin_id=plugin.id,
            name=plugin.name or plugin.id,
            # The repo, so a plugin whose options depend on current settings answers the same
            # whether or not the feature is switched on. See FeaturePlugin.config_schema.
            fields=plugin.config_schema(self._manager.config),
            values=values,
            # The key the page uses for `data-category`, so Back returns to the right view —
            # built by the same function the markup used, never re-derived here.
            category=category_key(plugin.group, self._category_names),
            accent=(style.accent_from, style.accent_to),
        )

    def _on_save_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Persist one plugin's settings from the page, and re-apply them.

        Returns:
            ``{}`` on success, or ``{"error": ...}`` — a save that could not be re-applied is
            still a save, and the panel says so rather than closing as though all was well.
        """
        plugin = self._plugin(str(data.get("id", "")))
        if plugin is None:
            return {"error": "That feature is no longer available."}
        values = data.get("values")
        if not isinstance(values, dict):
            return {"error": "Nothing to save."}
        # BEFORE the write, not after: `update_section` does not validate, and a rejected
        # value persisted here deactivates the plugin AND makes this panel unopenable — so the
        # one place that can still report the problem is this one, while it is still on screen.
        rejected = self._manager.config.section_rejects(plugin.id, values)
        if rejected:
            return {"error": f"Not saved — {rejected}."}
        try:
            self._manager.config.update_section(plugin.id, values)
        except Exception:
            logger.exception("settings: saving %s failed", plugin.id)
            return {"error": "Those settings could not be saved — see the Omnia log."}
        try:
            self._manager.reload(plugin.id)
        except Exception:
            logger.exception("settings: re-applying %s failed", plugin.id)
            self._push_card_state(plugin.id)
            return {
                "error": "Saved, but the feature could not be re-applied. See the log."
            }
        self._push_card_state(plugin.id)
        return {}

    def _watch_jobs(self) -> None:
        """Keep the Sync button and every plugin card showing what is running behind them.

        A background job outlives every window that can show it — that is the point of running
        it in the background — so this polls rather than being called back into: a closed dialog
        simply stops asking, where a callback held by one is a call into a deleted webview.

        ONE timer for both. Two would double the wake-ups for the same 500ms of nothing
        happening, and would let the Sync button and the cards disagree about what "now" is.
        """
        from aqt import mw

        if self._sync_timer is None:
            self._sync_timer = mw.progress.timer(500, self._tick, True, parent=self)
        self._tick()

    def _tick(self) -> None:
        """One poll: the sync button, then every plugin card."""
        self._sync_tick()
        self._jobs_tick()

    def _sync_tick(self) -> None:
        from omnia.core.sync.progress import DONE, FAILED
        from omnia.gui.sync.job import current

        job = current()
        if job is None:
            self._push_sync_progress(None, "")
            return
        progress = job.snapshot()
        finished = progress.phase in (DONE, FAILED)
        self._push_sync_progress(
            None if finished else progress.percent,
            job.result if finished else progress.summary(),
        )

    def _jobs_tick(self) -> None:
        """Keep every plugin card showing whatever its plugin says it is doing.

        Generic on purpose: this asks each card's plugin for ``<id>.progress`` and renders what
        answers, so a plugin gains a progress bar by publishing a
        :class:`~omnia.core.progress.JobTracker` and this file learns nothing about it. A plugin
        that publishes none is simply not drawn — ``lookup`` answering ``None`` is the whole
        answer.

        Polled, like the sync watcher and for the same reason: the job outlives every window
        that can show it, so a closed dialog stops asking where a callback held by one is a call
        into a deleted webview.
        """
        from omnia.core import services
        from omnia.core.progress import progress_service

        for plugin in self._manager.plugins():
            tracker = services.lookup(progress_service(plugin.id))
            if tracker is None:
                # Either the plugin has no jobs, or it is switched off and withdrew the
                # service mid-run. Clear whatever was last drawn rather than leaving a stale
                # bar sitting at 60% forever.
                self._push_card_progress(plugin.id, None, "", False)
                continue
            try:
                progress = tracker.snapshot()
            except Exception:  # pragma: no cover - a third-party tracker is not ours
                logger.exception("settings: %s reported unreadable progress", plugin.id)
                continue
            self._push_card_progress(
                plugin.id,
                progress.percent,
                progress.summary(),
                bool(progress.active and not progress.cancelled),
            )

    def _push_card_progress(
        self, plugin_id: str, percent: Any, text: str, stoppable: bool
    ) -> None:
        if self._jobs_shown.get(plugin_id) == (percent, text, stoppable):
            return  # nothing changed; do not repaint the page for it
        self._jobs_shown[plugin_id] = (percent, text, stoppable)
        self.eval_js(
            "window.omniaSettings.setCardProgress("
            + json.dumps(plugin_id)
            + ", "
            + json.dumps(percent)
            + ", "
            + json.dumps(text)
            + ", "
            + json.dumps(stoppable)
            + ");"
        )

    def _on_stop_job(self, data: dict[str, Any]) -> dict[str, Any]:
        """Ask one plugin's running job to stop.

        Asks; it does not force. The job stops where stopping is safe — for a batch that is
        between notes, so none is left half-generated — which is why the button goes to
        "Stopping…" rather than straight to gone.
        """
        from omnia.core import services
        from omnia.core.progress import progress_service

        tracker = services.lookup(progress_service(str(data.get("id", ""))))
        cancel = getattr(tracker, "cancel", None)
        if callable(cancel):
            cancel()
        self._jobs_tick()
        return {}

    def _push_sync_progress(self, percent: Any, tip: str) -> None:
        if (percent, tip) == self._sync_shown:
            return  # nothing changed; do not repaint the page for it
        self._sync_shown = (percent, tip)
        self.eval_js(
            "window.omniaSettings.setActionProgress("
            + json.dumps("sync")
            + ", "
            + json.dumps(percent)
            + ", "
            + json.dumps(tip)
            + ");"
        )

    def _on_sync(self, _data: dict[str, Any]) -> None:
        """Open the Sync panel — the one action tile, which belongs to no plugin.

        Deferred for the same reason :meth:`_on_configure` is: opening a webview dialog
        synchronously from inside this webview's bridge callback leaves the nested
        ``AnkiWebView`` loaded but never composited, which reads as a blank window.
        """
        from aqt.qt import QTimer

        QTimer.singleShot(0, self._open_sync)

    def _open_sync(self) -> None:
        from omnia.gui.sync.dialog import open_sync_dialog

        open_sync_dialog(self._manager.config, self)

    def _open_custom_dialog(self, plugin: FeaturePlugin) -> None:
        """Open a plugin's own dialog (it owns its persistence), then re-apply it."""
        dialog = plugin.custom_config_dialog(self._manager.config, self)
        if dialog is not None and dialog.exec():
            self._manager.reload(plugin.id)
            self._push_card_state(plugin.id)

    def _push_card_state(self, plugin_id: str) -> None:
        """Re-state one card in the open page after its config changed.

        A reload can fail — a setting the plugin refuses leaves it inactive — and the page has
        no idea, so the card would keep saying "active" and its category tile would keep
        counting it. Re-rendering the whole page would work but would throw the reader back to
        the landing, so only the card that was configured is pushed.
        """
        enabled = self._manager.config.is_enabled(plugin_id)
        active = self._manager.is_active(plugin_id)
        state = json.dumps(
            {
                "id": plugin_id,
                "enabled": enabled,
                "active": active,
                "status": status_text(enabled=enabled, active=active),
            }
        )
        self.eval_js(f"window.omniaSettings.setCardState({state});")

    def showEvent(self, evt: Any) -> None:  # noqa: N802 (Qt override name)
        """Start watching once the page is on screen.

        Here rather than in ``__init__``: the page has to exist before anything can be pushed
        into it, and a pull — or a batch — may already have been running before this window was
        opened. That is the ordinary case for the background batch: it is started from the
        browser, and this window is opened afterwards to see how it is getting on.
        """
        super().showEvent(evt)
        self._watch_jobs()
