"""A generic config dialog that renders a plugin's ``config_schema()`` into a form.

Each :class:`~omnia.core.plugin.ConfigField` maps to a Qt widget by ``kind`` (bool→checkbox,
int→spinbox, float→double spinbox, text/secret→line edit, choice→combo, color→colour picker).
This is how every feature gets a settings panel without a bespoke dialog — declare fields,
get a form.
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QCheckBox,
    QColor,
    QColorDialog,
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
    QPushButton,
    QSize,
    QSpinBox,
    Qt,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

_INFO_ICON: QIcon | None = None


def _help_html(text: str) -> str:
    """Escape ``text`` and turn newlines into ``<br>`` so Qt tooltips keep explicit breaks.

    Plain-text tooltips let Qt auto-wrap and collapse ``\\n``; wrapping the help as HTML (Qt
    auto-detects the ``<br>`` as rich text) preserves the authored line breaks while long
    lines still wrap.
    """
    return _html.escape(text).replace("\n", "<br>")


class _ColorButton(QPushButton):
    """A colour-picker button: shows the current hex, opens ``QColorDialog`` on click.

    Stores the selected colour as a ``#rrggbb`` hex string; the button text is that hex and
    its background is the colour itself, with black/white text chosen by luminance so the hex
    stays readable on any colour.
    """

    def __init__(self, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hex = value or "#000000"
        self.clicked.connect(self._pick)
        self._refresh()

    def hex(self) -> str:
        """Return the currently selected colour as a ``#rrggbb`` hex string."""
        return self._hex

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._hex), self)
        if chosen.isValid():
            self._hex = chosen.name()
            self._refresh()

    def _refresh(self) -> None:
        color = QColor(self._hex)
        # Perceived luminance (ITU-R BT.601): dark text on light colours, light on dark.
        luminance = (
            0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        ) / 255
        fg = "#000000" if luminance >= 0.5 else "#ffffff"
        self.setText(self._hex)
        self.setStyleSheet(f"background-color:{self._hex}; color:{fg};")


def _info_icon() -> QIcon:
    """A small filled-circle 'i' info icon (drawn once; cached). Accent blue reads on both themes."""
    global _INFO_ICON
    if _INFO_ICON is not None:
        return _INFO_ICON
    px = 44  # draw big, display small → crisp on retina
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#5b6ef5"))
    p.drawEllipse(1, 1, px - 2, px - 2)
    # Pure-white, extra-bold, larger glyph so the "i" reads clearly even at the 16px display
    # size (the previous thin glyph was hard to make out).
    p.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPixelSize(int(px * 0.74))
    font.setWeight(QFont.Weight.Black)
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
                # The clickable (i) icon in the value row is the SINGLE help source — a
                # width-limited, wrapped tooltip plus click-to-show. Deliberately NO tooltip on
                # the label or the widget: those were unbounded and popped a screen-wide
                # one-line strip on hover/click, duplicating the (i).
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
        # lines instead of one screen-wide strip; _help_html also keeps authored line breaks.
        rich = (
            "<div style='max-width:320px; font-size:13px; line-height:1.45;'>"
            f"{_help_html(help_text)}</div>"
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
            # A legit ``0`` bound is falsy, so test ``is None`` — ``field.maximum or DEFAULT``
            # would treat a real 0 as "unset" and widen the range past it.
            minimum = 0 if field.minimum is None else int(field.minimum)
            maximum = 1_000_000 if field.maximum is None else int(field.maximum)
            w.setRange(minimum, maximum)
            w.setValue(int(value or 0))
            return w
        if field.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setSingleStep(0.1)
            minimum = 0.0 if field.minimum is None else float(field.minimum)
            maximum = 1_000_000.0 if field.maximum is None else float(field.maximum)
            w.setRange(minimum, maximum)
            w.setValue(float(value or 0.0))
            return w
        if field.kind == "choice":
            w = QComboBox()
            w.addItems(list(field.choices))
            # settings.dict() (pydantic v1 without use_enum_values) hands back an Enum MEMBER,
            # not its string value, so a raw ``value in field.choices`` (stringy choices) misses
            # and the field silently resets to index 0. Normalize to the underlying value first.
            normalized = str(getattr(value, "value", value))
            # Preserve an out-of-range stored value as its own option instead of silently
            # coercing to index 0 — otherwise OK would overwrite the user's real value on save.
            if normalized and normalized not in field.choices:
                w.addItem(normalized)
            if normalized:
                w.setCurrentText(normalized)
            return w
        if field.kind == "color":
            return _ColorButton(str(value or ""))
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
            elif isinstance(widget, _ColorButton):
                # A _ColorButton is a QPushButton (not a QLineEdit), so read its hex explicitly.
                result[field.key] = widget.hex()
            elif isinstance(widget, QLineEdit):
                result[field.key] = widget.text()
        return result
