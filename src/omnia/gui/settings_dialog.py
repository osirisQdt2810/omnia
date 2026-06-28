"""The Omnia settings dialog — a grouped, animated webview list of feature plugins.

Built on the reusable :class:`~omnia.gui.web_dialog.WebDialog` seam: the whole UI is HTML/CSS/
JS rendered inside an ``AnkiWebView`` (gradients, animated toggle switches, hover lift, and
tooltips — beyond what raw Qt stylesheets allow). The page is built by the pure
``settings_html`` module; this class is the thin Qt glue that supplies the view-model and
handles the two ``pycmd`` ops (``toggle`` / ``configure``). Only loaded inside Anki.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.theme import theme_manager

from omnia.core.manager import grouped_plugins
from omnia.gui.config_form import PluginConfigDialog
from omnia.gui.settings_html import PluginCardModel, build_settings_html, status_text
from omnia.gui.web_dialog import WebDialog

if TYPE_CHECKING:
    from omnia.core.manager import PluginManager
    from omnia.core.plugin import FeaturePlugin


class SettingsDialog(WebDialog):
    """Lists every feature plugin, grouped, with a live toggle and a Configure button."""

    def __init__(self, manager: PluginManager, parent: Any = None) -> None:
        self._manager = manager
        super().__init__(
            parent,
            title="Omnia — All-in-One Toolkit",
            html=self._render(),
            handlers={"toggle": self._on_toggle, "configure": self._on_configure},
            width=620,
            height=580,
        )

    def _render(self) -> str:
        groups = [
            (name, [self._card(plugin) for plugin in plugins])
            for name, plugins in grouped_plugins(self._manager)
        ]
        return build_settings_html(groups, dark=theme_manager.night_mode)

    def _card(self, plugin: FeaturePlugin) -> PluginCardModel:
        return PluginCardModel(
            id=plugin.id,
            name=plugin.name or plugin.id,
            description=plugin.description,
            tooltip=plugin.tooltip or plugin.description,
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

    def _on_configure(self, data: dict[str, Any]) -> None:
        """Open the plugin's config dialog (custom first, else the generic form), then reload."""
        plugin_id = str(data.get("id", ""))
        plugin = next((p for p in self._manager.plugins() if p.id == plugin_id), None)
        if plugin is not None:
            self._configure(plugin)

    def _configure(self, plugin: FeaturePlugin) -> None:
        # A bespoke dialog (it owns its own persistence via the repo) takes precedence over the
        # generic ConfigField form; reload the plugin afterwards so changes apply if active.
        if plugin.has_custom_config_dialog():
            dialog = plugin.custom_config_dialog(self._manager.config, self)
            if dialog is not None and dialog.exec():
                self._manager.reload(plugin.id)
            return
        settings = self._manager.config.feature_settings(plugin.id)
        current = settings.dict() if settings is not None else {}
        dialog = PluginConfigDialog(
            plugin.name or plugin.id, plugin.config_schema(), current, self
        )
        if dialog.exec():
            self._manager.config.update_section(plugin.id, dialog.values())
            self._manager.reload(plugin.id)  # re-apply with the new settings if active
