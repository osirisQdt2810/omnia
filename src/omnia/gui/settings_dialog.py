"""The Omnia settings dialog — a clean, card-based list of feature plugins with toggles.

Each registered plugin is shown as a card with its name, description, and an enable switch.
Toggling a switch applies immediately through the :class:`~omnia.core.manager.PluginManager`
(activate/deactivate + persist). Pure Qt glue — only loaded inside Anki.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt.qt import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    Qt,
    QVBoxLayout,
    QWidget,
)

from omnia.gui.config_form import PluginConfigDialog

if TYPE_CHECKING:
    from omnia.core.manager import PluginManager
    from omnia.core.plugin import FeaturePlugin

_STYLE = """
#omniaHeader { font-size: 20px; font-weight: 700; }
#omniaSub { color: palette(mid); }
QFrame#card {
    border: 1px solid palette(mid);
    border-radius: 10px;
    padding: 10px 12px;
    background: palette(base);
}
QFrame#card:hover { border-color: palette(highlight); }
#cardTitle { font-size: 14px; font-weight: 600; }
#cardDesc { color: palette(mid); }
#cardStatus { color: palette(mid); font-size: 11px; }
"""


class SettingsDialog(QDialog):
    """Lists every feature plugin with a toggle that enables/disables it live."""

    def __init__(self, manager: PluginManager, parent: object | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("Omnia — All-in-One Toolkit")
        self.setMinimumWidth(540)
        self.setMinimumHeight(520)
        self.setStyleSheet(_STYLE)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(6)

        header = QLabel("Omnia")
        header.setObjectName("omniaHeader")
        outer.addWidget(header)

        subtitle = QLabel("Tick a feature to turn it on — changes apply immediately.")
        subtitle.setObjectName("omniaSub")
        outer.addWidget(subtitle)
        outer.addSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        self._list = QVBoxLayout(container)
        self._list.setSpacing(10)
        self._list.setContentsMargins(0, 0, 6, 0)
        for plugin in self._manager.plugins():
            self._list.addWidget(self._plugin_card(plugin))
        self._list.addStretch(1)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

    def _plugin_card(self, plugin: FeaturePlugin) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setSpacing(12)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title = QLabel(plugin.name or plugin.id)
        title.setObjectName("cardTitle")
        desc = QLabel(plugin.description)
        desc.setObjectName("cardDesc")
        desc.setWordWrap(True)
        status = QLabel(self._status_text(plugin.id))
        status.setObjectName("cardStatus")
        text_col.addWidget(title)
        text_col.addWidget(desc)
        text_col.addWidget(status)
        row.addLayout(text_col, 1)

        # A "Configure" button appears only when the plugin declares options.
        if plugin.config_schema():
            configure = QPushButton("Configure…")
            configure.setCursor(Qt.CursorShape.PointingHandCursor)
            configure.clicked.connect(lambda _=False, p=plugin: self._configure(p))
            row.addWidget(configure, 0, Qt.AlignmentFlag.AlignTop)

        toggle = QCheckBox()
        toggle.setChecked(self._manager.config.is_enabled(plugin.id))
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.toggled.connect(
            lambda checked, pid=plugin.id, lbl=status: self._on_toggle(
                pid, checked, lbl
            )
        )
        row.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _configure(self, plugin: FeaturePlugin) -> None:
        settings = self._manager.config.feature_settings(plugin.id)
        current = settings.model_dump() if settings is not None else {}
        dialog = PluginConfigDialog(
            plugin.name or plugin.id, plugin.config_schema(), current, self
        )
        if dialog.exec():
            self._manager.config.update_section(plugin.id, dialog.values())
            self._manager.reload(plugin.id)  # re-apply with the new settings if active

    def _status_text(self, plugin_id: str) -> str:
        return "● active" if self._manager.is_active(plugin_id) else "○ off"

    def _on_toggle(self, plugin_id: str, checked: bool, status_label: QLabel) -> None:
        try:
            active = self._manager.set_enabled(plugin_id, checked)
        except KeyError:
            status_label.setText("⚠ unknown plugin")
            return
        if checked and not active:
            status_label.setText("⚠ failed to enable — see logs")
        else:
            status_label.setText(self._status_text(plugin_id))
