"""A generic config dialog that renders a plugin's ``config_schema()`` into a form.

Each :class:`~omnia.core.plugin.ConfigField` maps to a Qt widget by ``kind`` (bool→checkbox,
int→spinbox, float→double spinbox, text/secret→line edit, choice→combo). This is how every
feature gets a settings panel without a bespoke dialog — declare fields, get a form.
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QCheckBox,
    QColor,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFont,
    QFormLayout,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QPainter,
    QPixmap,
    QPoint,
    QSize,
    QSpinBox,
    Qt,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

_INFO_ICON: QIcon | None = None


def _info_icon() -> QIcon:
    """A small filled-circle 'i' info icon (drawn once; cached). Accent blue reads on both themes."""
    global _INFO_ICON
    if _INFO_ICON is not None:
        return _INFO_ICON
    px = 40  # draw big, display small → crisp on retina
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#5b6ef5"))
    p.drawEllipse(1, 1, px - 2, px - 2)
    p.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPixelSize(int(px * 0.66))
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "i")
    p.end()
    _INFO_ICON = QIcon(pm)
    return _INFO_ICON


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
        """Field cell: a clickable (i) info button, then the value control.

        The icon sits at the start of the value column (right after the label, before the
        value) so the icons line up in a column; a gap separates it from both.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Wrap the help as width-limited HTML so a long tooltip wraps onto several readable
        # lines instead of one screen-wide strip.
        rich = (
            "<div style='max-width:320px; font-size:13px; line-height:1.45;'>"
            f"{_html.escape(help_text)}</div>"
        )

        info = QToolButton()
        info.setIcon(_info_icon())
        info.setIconSize(QSize(16, 16))
        info.setToolTip(rich)
        info.setCursor(Qt.CursorShape.PointingHandCursor)
        info.setAutoRaise(True)
        info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Flat, borderless — just the circular icon, no square button frame.
        info.setStyleSheet(
            "QToolButton{border:none;background:transparent;padding:0;margin:0;}"
            "QToolButton:hover,QToolButton:pressed{border:none;background:transparent;}"
        )
        info.setAccessibleName("Field help")
        # Click → show the help right at the icon (independent of the hover-tooltip delay).
        info.clicked.connect(
            lambda _=False, b=info, t=rich: QToolTip.showText(
                b.mapToGlobal(QPoint(0, b.height())), t, b
            )
        )
        layout.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(10)  # gap between the icon and the value
        layout.addWidget(widget, 1)
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
