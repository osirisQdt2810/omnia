"""Tests for the note-maintenance dialogs' non-Qt parts (real Qt isn't available headless).

Both modules bind their ``aqt.qt`` symbols at module top, so the stub below supplies the names
they import — which also makes importing them a smoke test: these two files are the only ones
in the package no other test can load. What is actually asserted is the piece with logic in it:
the diff palette, which turns ``diff.py``'s semantic ``<del>``/``<ins>`` marks into the inline
styles Qt's rich text engine understands, per theme.
"""

from __future__ import annotations

import sys
import types

_qt = sys.modules["aqt.qt"]
for _name in (
    "QAbstractItemView",
    "QCheckBox",
    "QDialog",
    "QDialogButtonBox",
    "QDoubleSpinBox",
    "QFormLayout",
    "QFrame",
    "QHBoxLayout",
    "QHeaderView",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QPalette",
    "QPushButton",
    "QScrollArea",
    "QSpinBox",
    "QStackedWidget",
    "Qt",
    "QTableWidget",
    "QTableWidgetItem",
    "QVBoxLayout",
    "QWidget",
):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, type(_name, (), {}))

# Reuse the registered stub if another gui test installed one first — mutating a copy that
# ISN'T in sys.modules would leave the module under test reading a different theme_manager.
_theme = sys.modules.setdefault("aqt.theme", types.ModuleType("aqt.theme"))
if not hasattr(_theme, "theme_manager"):
    _theme.theme_manager = types.SimpleNamespace(night_mode=False)

import aqt  # noqa: E402  (the conftest stub package)

aqt.theme = _theme

from omnia.gui.note_maintenance import panel  # noqa: E402,F401  (import = smoke test)
from omnia.gui.note_maintenance.preview_dialog import (  # noqa: E402
    _DiffPalette,
)


class TestDiffPalette:
    def test_marks_become_styled_spans(self):
        palette = _DiffPalette(removed="R", added="A")

        assert (
            palette.rich("keep <del>gone</del>") == 'keep <span style="R">gone</span>'
        )
        assert palette.rich("keep <ins>new</ins>") == 'keep <span style="A">new</span>'

    def test_escaped_field_markup_is_left_alone(self):
        # diff.py escapes the field's own markup, so nothing in the text can be mistaken for
        # a diff mark and re-styled.
        palette = _DiffPalette(removed="R", added="A")

        assert palette.rich("&lt;b&gt;bold&lt;/b&gt;") == "&lt;b&gt;bold&lt;/b&gt;"

    def test_the_palette_follows_the_active_theme(self, monkeypatch):
        monkeypatch.setattr(_theme.theme_manager, "night_mode", False)
        day = _DiffPalette.current()
        monkeypatch.setattr(_theme.theme_manager, "night_mode", True)
        night = _DiffPalette.current()

        assert day != night
        assert "line-through" in day.removed and "line-through" in night.removed
