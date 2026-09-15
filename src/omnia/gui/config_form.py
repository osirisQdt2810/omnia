"""Qt controls for a declared :class:`~omnia.core.plugin.ConfigField`.

Each field maps to a widget by ``kind`` (bool→checkbox, int→spinbox, float→double spinbox,
text/secret→line edit, choice→combo, color→colour picker).

This used to back a generic per-plugin settings DIALOG. It no longer does: a plugin's declared
options are rendered by the settings page itself (:mod:`omnia.gui.config_panel` decides the
controls, ``web/settings.js`` draws them), so pressing Configure no longer opens a second window
made of spin boxes next to a page made of gradients.

What is left is the BESPOKE-dialog case. The Note Maintenance per-task panel is its own Qt
dialog that still renders declared fields, and it reuses these controls rather than growing a
second widget factory. Both renderers read the same ``ConfigField`` list, so the schema is still
the one source of truth for what a plugin has — only the drawing differs, because the hosts do.
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QCheckBox,
    QColor,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFont,
    QIcon,
    QLineEdit,
    QPainter,
    QPixmap,
    QPushButton,
    QSpinBox,
    Qt,
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


class ConfigFieldEditor:
    """One :class:`ConfigField` rendered as a Qt control, plus how to read its value back.

    Owns BOTH directions of the ``kind`` mapping so they cannot drift apart: what
    :meth:`_build` draws for a kind is what :meth:`value` reads it back as. Shared by the
    generic :class:`PluginConfigDialog` and by bespoke dialogs that render declared fields of
    their own (``gui/note_maintenance/panel.py``), which is why it is a standalone class and
    not a method on the dialog.

    Attributes:
        field: The descriptor this editor renders.
        widget: The control — the caller places it in its own layout.
    """

    def __init__(self, field: ConfigField, value: Any) -> None:
        """Build the control for ``field``, showing ``value``.

        Args:
            field: The field descriptor (label, kind, bounds, choices).
            value: The current value to preselect.
        """
        self.field = field
        self.widget = self._build(field, value)

    def value(self) -> Any:
        """Return the control's current value, typed as the field's ``kind`` implies."""
        kind = self.field.kind
        if kind == "bool":
            return self.widget.isChecked()
        if kind in ("int", "float"):
            return self.widget.value()
        if kind == "choice":
            return self.widget.currentText()
        if kind == "color":
            return self.widget.hex()
        return self.widget.text()

    @staticmethod
    def _build(field: ConfigField, value: Any) -> QWidget:
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
