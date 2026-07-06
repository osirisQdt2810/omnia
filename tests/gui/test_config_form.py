"""Tests for the generic plugin config form's widget building.

``config_form`` imports many ``aqt.qt`` symbols at module top (real Qt isn't available
headless), so we extend conftest's ``aqt.qt`` stub with the names it binds — only QComboBox
needs real behaviour for the choice-preselect test; the rest are placeholders so the import
succeeds. The tested method (``_make_widget``) is a staticmethod, exercised without a QDialog.
"""

from __future__ import annotations

import sys
from enum import Enum

from omnia.core.plugin import ConfigField

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

    def setCurrentText(self, text) -> None:
        self._current = text

    def currentText(self) -> str:
        return self._current


for _name in (
    "QCheckBox",
    "QColor",
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
    "QSize",
    "QSpinBox",
    "Qt",
    "QToolButton",
    "QToolTip",
    "QVBoxLayout",
    "QWidget",
):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, object)
_qt.QComboBox = _FakeComboBox

from omnia.gui.config_form import PluginConfigDialog  # noqa: E402


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


class TestChoiceWidgetPreselect:
    """L10: an Enum-backed choice value must preselect, not silently reset to index 0."""

    def test_enum_member_preselects_its_value(self):
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, _Color.GREEN)
        assert widget.currentText() == "green"

    def test_string_non_first_value_preselects(self):
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, "blue")
        assert widget.currentText() == "blue"

    def test_unknown_value_falls_back_to_first(self):
        widget = PluginConfigDialog._make_widget(_CHOICE_FIELD, "purple")
        assert widget.currentText() == "red"
