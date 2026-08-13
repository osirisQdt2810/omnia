"""Tests for the note-maintenance dialogs' non-Qt parts (real Qt isn't available headless).

Both modules bind their ``aqt.qt`` symbols at module top, so the stub below supplies the names
they import — which also makes importing them a smoke test: these two files are the only ones
in the package no other test can load. What is actually asserted is the pieces with logic in
them: the diff palette, which turns ``diff.py``'s semantic ``<del>``/``<ins>`` marks into the
inline styles Qt's rich text engine understands, and the settings panel's option split, which
decides what a save writes back — including the options this version cannot render.
"""

from __future__ import annotations

import sys
import types
from typing import Any

from pydantic import Field

_qt = sys.modules["aqt.qt"]
for _name in (
    "QAbstractItemView",
    "QApplication",
    "QCheckBox",
    "QDialog",
    "QDialogButtonBox",
    "QDoubleSpinBox",
    "QFormLayout",
    "QHBoxLayout",
    "QHeaderView",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QPalette",
    "QPushButton",
    "QSize",
    "QSpinBox",
    "QStackedWidget",
    "QStyle",
    "QStyledItemDelegate",
    "QStyleOptionViewItem",
    "Qt",
    "QTableWidget",
    "QTableWidgetItem",
    "QTextDocument",
    "QTreeWidget",
    "QTreeWidgetItem",
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

from omnia.gui.note_maintenance.panel import _TaskOptions  # noqa: E402
from omnia.gui.note_maintenance.preview_dialog import (  # noqa: E402
    _DiffPalette,
)
from omnia.plugins.note_maintenance.base import TaskConfigBase  # noqa: E402


class _DemoConfig(TaskConfigBase):
    """A task config with one option of each kind the panel has to deal with."""

    threshold: float = 0.5
    fields: dict[str, str] = Field(default_factory=lambda: {"From": "To"})
    # No renderer knows a list — this is what a save must not silently drop.
    extras: list[str] = Field(default_factory=lambda: ["kept"])


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

    def test_a_cell_carries_both_lines_styled(self):
        palette = _DiffPalette(removed="R", added="A")

        cell = palette.cell("old <del>word</del>", "old <ins>text</ins>")

        assert cell == (
            '<div>− old <span style="R">word</span></div>'
            '<div>+ old <span style="A">text</span></div>'
        )


class TestTaskOptions:
    """What the settings panel renders, and what it has to carry through a save untouched."""

    def _split(self, **overrides: Any) -> _TaskOptions:
        return _TaskOptions(_DemoConfig(**overrides))

    def test_the_tick_owns_enable_so_the_form_never_renders_it(self):
        assert "enable" not in [name for name, _value, _field in self._split().rows]
        assert "enable" not in self._split().passthrough

    def test_scalars_are_rendered_with_their_descriptor(self):
        rows = {name: field for name, _value, field in self._split().rows}

        assert rows["threshold"] is not None
        assert rows["threshold"].kind == "float"

    def test_a_field_mapping_is_rendered_by_the_bespoke_editor(self):
        rows = {name: field for name, _value, field in self._split().rows}

        assert "fields" in rows and rows["fields"] is None

    def test_an_option_no_renderer_knows_is_kept_verbatim(self):
        # The ADR-010 hazard one layer up: writing back only what the form could draw would
        # delete a task option a NEWER Omnia added, on the next sync.
        split = self._split(extras=["one", "two"])

        assert split.passthrough == {"extras": ["one", "two"]}
        assert "extras" not in [name for name, _value, _field in split.rows]

    def test_the_rows_keep_the_models_order(self):
        assert [name for name, _value, _field in self._split().rows] == [
            "order",
            "threshold",
            "fields",
        ]
