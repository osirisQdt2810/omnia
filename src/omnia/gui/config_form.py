"""A generic config dialog that renders a plugin's ``config_schema()`` into a form.

Each :class:`~omnia.core.plugin.ConfigField` maps to a Qt widget by ``kind`` (bool→checkbox,
int→spinbox, float→double spinbox, text/secret→line edit, choice→combo). This is how every
feature gets a settings panel without a bespoke dialog — declare fields, get a form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPoint,
    QSpinBox,
    Qt,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from omnia.core.plugin import ConfigField


class PluginConfigDialog(QDialog):
    """Edits a plugin's settings from its declared :class:`ConfigField` list."""

    def __init__(
        self,
        title: str,
        fields: list[ConfigField],
        current: dict[str, Any],
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} — settings")
        self.setMinimumWidth(420)
        self._fields = fields
        self._widgets: dict[str, QWidget] = {}
        self._build(current)

    def _build(self, current: dict[str, Any]) -> None:
        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)
        for field in self._fields:
            widget = self._make_widget(field, current.get(field.key, field.default))
            self._widgets[field.key] = widget
            label = QLabel(field.label)
            if field.help:
                # Hover tooltip AND an always-visible clickable (i) icon — hover tooltips are
                # easy to miss (esp. on macOS), so the icon makes the help discoverable.
                label.setToolTip(field.help)
                widget.setToolTip(field.help)
                form.addRow(label, self._field_row(widget, field.help))
            else:
                form.addRow(label, widget)
        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _field_row(widget: QWidget, help_text: str) -> QWidget:
        """Wrap ``widget`` with a trailing clickable (i) info button showing ``help_text``."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(widget, 1)

        info = QToolButton()
        info.setText("ⓘ")
        info.setToolTip(help_text)
        info.setCursor(Qt.CursorShape.PointingHandCursor)
        info.setAutoRaise(True)
        info.setAccessibleName("Field help")
        # Click → show the help right at the icon (independent of the hover-tooltip delay).
        info.clicked.connect(
            lambda _=False, b=info, t=help_text: QToolTip.showText(
                b.mapToGlobal(QPoint(0, b.height())), t, b
            )
        )
        layout.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    @staticmethod
    def _make_widget(field: ConfigField, value: Any) -> QWidget:
        if field.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if field.kind == "int":
            w = QSpinBox()
            w.setRange(int(field.minimum or 0), int(field.maximum or 1_000_000))
            w.setValue(int(value or 0))
            return w
        if field.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setSingleStep(0.1)
            w.setRange(float(field.minimum or 0.0), float(field.maximum or 1_000_000.0))
            w.setValue(float(value or 0.0))
            return w
        if field.kind == "choice":
            w = QComboBox()
            w.addItems(list(field.choices))
            if value in field.choices:
                w.setCurrentText(str(value))
            return w
        # text / secret
        w = QLineEdit(str(value or ""))
        if field.kind == "secret":
            w.setEchoMode(QLineEdit.EchoMode.Password)
        return w

    def values(self) -> dict[str, Any]:
        """Return the edited values keyed by field key."""
        result: dict[str, Any] = {}
        for field in self._fields:
            widget = self._widgets[field.key]
            if isinstance(widget, QCheckBox):
                result[field.key] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                result[field.key] = widget.value()
            elif isinstance(widget, QComboBox):
                result[field.key] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                result[field.key] = widget.text()
        return result
