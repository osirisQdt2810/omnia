"""Tests for the generic plugin config form's widget building.

``config_form`` imports many ``aqt.qt`` symbols at module top (real Qt isn't available
headless), so we extend conftest's ``aqt.qt`` stub with the names it binds. QComboBox needs
real behaviour for the choice-preselect test; QPushButton/QColor/QColorDialog need enough for
the ``_ColorButton`` picker; the check/spin widgets are DISTINCT classes (not a shared
``object``) so ``values()``' ``isinstance`` chain can tell a colour button apart from them.
The tested methods (``_make_widget`` staticmethod, ``values``) run without a real QDialog.
"""

from __future__ import annotations

import sys
import types
from enum import Enum

from omnia.core.config.schema import schema_from_model
from omnia.core.plugin import ConfigField
from omnia.plugins.display_interval.config import DisplayIntervalSettings

# --- extend the aqt.qt stub with the symbols config_form imports --------------------
_qt = sys.modules["aqt.qt"]


class _FakeComboBox:
    """Mimics QComboBox's preselect behaviour: addItems selects index 0 until setCurrentText."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._current = ""

    def addItems(self, items) -> None:
        self._items = list(items)
        if self._items and not self._current:
            self._current = self._items[0]

    def addItem(self, item) -> None:
        self._items.append(item)

    def setCurrentText(self, text) -> None:
        self._current = text

    def currentText(self) -> str:
        return self._current


class _FakeSpin:
    """Mimics QSpinBox: records the range so the ``0``-bound test can read it back."""

    def __init__(self) -> None:
        self._min = None
        self._max = None
        self._value = None

    def setRange(self, lo, hi) -> None:
        self._min = lo
        self._max = hi

    def setValue(self, v) -> None:
        self._value = v

    def value(self):
        return self._value


class _FakeDoubleSpin(_FakeSpin):
    """Mimics QDoubleSpinBox (distinct class); adds the float-only setters config_form calls."""

    def setDecimals(self, n) -> None:
        pass

    def setSingleStep(self, s) -> None:
        pass


class _FakeButton:
    """Minimal QPushButton stand-in so ``_ColorButton`` can be built headless."""

    def __init__(self, parent=None) -> None:
        self._text = ""
        self._style = ""
        self.clicked = types.SimpleNamespace(connect=lambda *_a, **_k: None)

    def setText(self, text) -> None:
        self._text = text

    def setStyleSheet(self, style) -> None:
        self._style = style

    def setToolTip(self, text) -> None:
        pass


class _FakeColor:
    """Minimal QColor stand-in: constructed from a hex string; RGB channels for luminance."""

    def __init__(self, value="") -> None:
        self._value = value

    def red(self) -> int:
        return 0

    def green(self) -> int:
        return 0

    def blue(self) -> int:
        return 0

    def name(self) -> str:
        return self._value

    def isValid(self) -> bool:
        return True


class _FakeColorDialog:
    """Minimal QColorDialog stand-in (never actually opened in the headless test)."""

    @staticmethod
    def getColor(initial=None, parent=None):
        return initial


# Distinct placeholder classes (not a shared ``object``) for the symbols config_form binds at
# import, so values()' isinstance chain discriminates widget types; only set if unset so this
# never clobbers another test module's richer stub.
for _name in (
    "QCheckBox",
    "QColor",
    "QColorDialog",
    "QDialog",
    "QDialogButtonBox",
    "QDoubleSpinBox",
    "QFont",
    "QFormLayout",
    "QHBoxLayout",
    "QIcon",
    "QLabel",
    "QLineEdit",
    "QPainter",
    "QPixmap",
    "QPoint",
    "QPushButton",
    "QSize",
    "QSpinBox",
    "Qt",
    "QToolButton",
    "QToolTip",
    "QVBoxLayout",
    "QWidget",
):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, type(_name, (), {}))
# Functional stubs the widgets actually exercise (force-set: config_form is the only importer).
_qt.QComboBox = _FakeComboBox
_qt.QPushButton = _FakeButton
_qt.QColor = _FakeColor
_qt.QColorDialog = _FakeColorDialog
_qt.QSpinBox = _FakeSpin
_qt.QDoubleSpinBox = _FakeDoubleSpin

from omnia.gui.config_form import PluginConfigDialog, _ColorButton  # noqa: E402


class _Color(Enum):
    """A plain (non-str) Enum, as pydantic v1 hands back without use_enum_values."""

    RED = "red"
    GREEN = "green"
    BLUE = "blue"


_CHOICE_FIELD = ConfigField(
    key="color",
    label="Color",
    kind="choice",
    default="red",
    choices=("red", "green", "blue"),
)

_COLOR_FIELD = ConfigField(
    key="text_color",
    label="Text color",
    kind="color",
    default="#c62828",
)


class TestChoiceWidgetPreselect:
    """L10: an Enum-backed choice value must preselect, not silently reset to index 0."""

    def test_enum_member_preselects_its_value(self):
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, _Color.GREEN)
        assert widget.currentText() == "green"

    def test_string_non_first_value_preselects(self):
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, "blue")
        assert widget.currentText() == "blue"

    def test_unknown_value_is_preserved(self):
        # An out-of-range stored value must be kept (appended as its own option) and selected,
        # not silently coerced to index 0 — otherwise OK would overwrite the user's real value.
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, "purple")
        assert widget.currentText() == "purple"
        assert "purple" in widget._items


class TestNumericBounds:
    """A legit ``0`` bound must be honored, not treated as unset (``is None``, not truthiness)."""

    def test_int_zero_maximum_is_honored(self):
        field = ConfigField(
            key="offset", label="Offset", kind="int", default=0, minimum=-10, maximum=0
        )
        widget = PluginConfigDialog._make_widget(field, -3)
        assert widget._min == -10
        assert widget._max == 0

    def test_int_unset_maximum_uses_default(self):
        field = ConfigField(key="n", label="N", kind="int", default=0)
        widget = PluginConfigDialog._make_widget(field, 5)
        assert widget._min == 0
        assert widget._max == 1_000_000

    def test_float_zero_maximum_is_honored(self):
        field = ConfigField(
            key="ratio",
            label="Ratio",
            kind="float",
            default=0.0,
            minimum=-1.0,
            maximum=0.0,
        )
        widget = PluginConfigDialog._make_widget(field, -0.5)
        assert widget._min == -1.0
        assert widget._max == 0.0


class TestColorWidget:
    """A ``color`` field renders a picker button that round-trips its hex through values()."""

    def test_make_widget_preselects_default_hex(self):
        widget = PluginConfigDialog._make_widget(_COLOR_FIELD, "#c62828")
        assert isinstance(widget, _ColorButton)
        assert widget.hex() == "#c62828"

    def test_color_field_derived_from_model(self):
        color = self._color_field()
        assert color.kind == "color"
        assert color.default == "#c62828"

    def test_values_returns_selected_hex(self):
        # Build the field list from the real model so the color kind flows end-to-end, then
        # read a picked colour back. values() runs on a bypass-built dialog (no Qt layout
        # stack headless), and the pick is simulated by setting the hex directly.
        color = self._color_field()
        widget = PluginConfigDialog._make_widget(color, color.default)
        widget._hex = "#00ff00"  # simulate a pick without opening a real QColorDialog
        dialog = PluginConfigDialog.__new__(PluginConfigDialog)
        dialog._fields = [color]
        dialog._widgets = {color.key: widget}
        assert dialog.values() == {"text_color": "#00ff00"}

    @staticmethod
    def _color_field() -> ConfigField:
        fields = schema_from_model(DisplayIntervalSettings)
        return next(field for field in fields if field.key == "text_color")
