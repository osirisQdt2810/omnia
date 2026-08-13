"""Tests for the note-maintenance dialogs' non-Qt parts (real Qt isn't available headless).

Both modules bind their ``aqt.qt`` symbols at module top, so the stub below supplies the names
they import — which also makes importing them a smoke test: these two files are the only ones
in the package no other test can load. What is actually asserted is the pieces with logic in
them: the diff palette, which turns ``diff.py``'s semantic ``<del>``/``<ins>`` marks into the
inline styles Qt's rich text engine understands, and the settings panel's save, which decides
what survives in the stored config — including the tasks and options this version cannot show.
"""

from __future__ import annotations

import enum
import sys
import types
from typing import Any

import pytest
from pydantic import Field

_qt = sys.modules["aqt.qt"]
for _name in (
    "QAbstractItemView",
    "QApplication",
    "QCheckBox",
    "QColor",
    "QColorDialog",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QDoubleSpinBox",
    "QFont",
    "QFormLayout",
    "QHBoxLayout",
    "QHeaderView",
    "QIcon",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QPainter",
    "QPalette",
    "QPixmap",
    "QPoint",
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
    "QToolButton",
    "QToolTip",
    "QTreeWidget",
    "QTreeWidgetItem",
    "QVBoxLayout",
    "QWidget",
):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, type(_name, (), {}))


class _CheckState(enum.Enum):
    """``Qt.CheckState``: what the panel compares a task's tick against."""

    Unchecked = 0
    PartiallyChecked = 1
    Checked = 2


# Additive on whichever ``Qt`` stub is registered (the panel binds THAT object at import).
if not hasattr(_qt.Qt, "CheckState"):
    _qt.Qt.CheckState = _CheckState

# Reuse the registered stub if another gui test installed one first — mutating a copy that
# ISN'T in sys.modules would leave the module under test reading a different theme_manager.
_theme = sys.modules.setdefault("aqt.theme", types.ModuleType("aqt.theme"))
if not hasattr(_theme, "theme_manager"):
    _theme.theme_manager = types.SimpleNamespace(night_mode=False)

import aqt  # noqa: E402  (the conftest stub package)

aqt.theme = _theme

from omnia.gui.note_maintenance.panel import (  # noqa: E402
    NoteMaintenanceSettingsDialog,
    _TaskOptions,
)
from omnia.gui.note_maintenance.preview_dialog import (  # noqa: E402
    _DIFF_COLUMN,
    _DiffPalette,
    _PreviewTree,
)
from omnia.plugins.note_maintenance.base import (  # noqa: E402
    MaintenanceTask,
    TaskConfigBase,
)

_PLUGIN_ID = "note_maintenance"


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


class TestPreviewTreeRelayout:
    """A column drag must cost ONE relayout, not one per pixel (every row is re-measured)."""

    def _tree(self, scheduled: list) -> Any:
        # __new__: the tree is a QTreeWidget, which needs a real Qt. Only the debounce state
        # the two methods touch is filled in.
        tree = _PreviewTree.__new__(_PreviewTree)
        tree._relayout_pending = False
        tree.doItemsLayout = lambda: scheduled.append("laid out")
        return tree

    def test_a_burst_of_resizes_schedules_one_pass(self, monkeypatch):
        timers: list = []
        monkeypatch.setattr(_qt.QTimer, "singleShot", lambda _ms, fn: timers.append(fn))
        done: list = []
        tree = self._tree(done)

        for _ in range(50):
            tree._on_section_resized(_DIFF_COLUMN, 0, 0)

        assert len(timers) == 1
        timers[0]()  # the drag settles
        assert done == ["laid out"]

    def test_a_later_drag_schedules_again(self, monkeypatch):
        timers: list = []
        monkeypatch.setattr(_qt.QTimer, "singleShot", lambda _ms, fn: timers.append(fn))
        tree = self._tree([])

        tree._on_section_resized(_DIFF_COLUMN, 0, 0)
        timers[0]()
        tree._on_section_resized(_DIFF_COLUMN, 0, 0)

        assert len(timers) == 2

    def test_the_label_column_never_schedules(self, monkeypatch):
        timers: list = []
        monkeypatch.setattr(_qt.QTimer, "singleShot", lambda _ms, fn: timers.append(fn))

        self._tree([])._on_section_resized(0, 0, 0)

        assert timers == []


class _FakeTaskItem:
    """The task list's row reduced to what the save reads: its tick."""

    def __init__(self, checked: bool) -> None:
        self._checked = checked

    def checkState(self) -> Any:
        state = _CheckState.Checked if self._checked else _CheckState.Unchecked
        return getattr(_qt.Qt.CheckState, state.name)


class _FakeTaskList:
    """The task column: one :class:`_FakeTaskItem` per task, in the panel's own order."""

    def __init__(self, checked: list[bool]) -> None:
        self._items = [_FakeTaskItem(state) for state in checked]

    def item(self, row: int) -> _FakeTaskItem:
        return self._items[row]


class _FakeOptionsEditor:
    """Stands in for one task's form: it only has to report the options it would save."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = dict(values)

    def values(self) -> dict[str, Any]:
        return dict(self._values)


def _untouched_editor(task: MaintenanceTask) -> _FakeOptionsEditor:
    """What the real per-task form reports when the user edits nothing.

    Built through the panel's own :class:`_TaskOptions`, so a round trip exercises the same
    split the live form uses — the rendered rows plus what no renderer knows.
    """
    options = _TaskOptions(task.config)
    rendered = {name: value for name, value, _field in options.rows}
    return _FakeOptionsEditor({**options.passthrough, **rendered})


def _dialog(repo: Any) -> NoteMaintenanceSettingsDialog:
    """The dialog with its Qt half faked out, loaded from ``repo`` exactly as on open.

    ``__new__`` skips ``QDialog.__init__`` (no Qt headless); everything :meth:`_save` touches
    is then filled in the same order the real constructor does.
    """
    dialog = NoteMaintenanceSettingsDialog.__new__(NoteMaintenanceSettingsDialog)
    dialog._repo = repo
    dialog._saved_tasks = {}
    dialog._tasks = dialog._configured_tasks()
    dialog._task_list = _FakeTaskList([task.is_enabled for task in dialog._tasks])
    dialog._editors = {task.task_id: _untouched_editor(task) for task in dialog._tasks}
    dialog.accepted = []
    dialog.accept = lambda: dialog.accepted.append(True)
    return dialog


@pytest.fixture
def warnings(monkeypatch):
    """Capture the ``aqt.utils.showWarning`` messages the save surfaces."""
    messages: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "aqt.utils",
        types.SimpleNamespace(
            showWarning=lambda text, *_a, **_k: messages.append(text)
        ),
    )
    return messages


class TestSaveKeepsWhatItCannotShow:
    """``update_section`` replaces the whole ``tasks`` map, so the save must carry it all.

    Rebuilding that map from the registry alone deletes a section this build does not know —
    ADR-010's data shredder one layer up, and one the model-level fix cannot reach because the
    loss happens ABOVE the task's own model.
    """

    def _saved(self, repo) -> dict[str, Any]:
        return repo.feature_settings(_PLUGIN_ID).tasks

    def test_a_task_section_this_build_never_heard_of_survives(
        self, config_repo, warnings
    ):
        config_repo.update_section(
            _PLUGIN_ID, {"tasks": {"a_future_task": {"enable": True, "mode": "beta"}}}
        )

        _dialog(config_repo)._save()

        assert self._saved(config_repo)["a_future_task"] == {
            "enable": True,
            "mode": "beta",
        }

    def test_an_option_a_known_tasks_model_rejects_survives(
        self, config_repo, warnings
    ):
        # The section falls back to that task's defaults for the OPTIONS it cannot read; the
        # value itself still has to reach storage untouched, or the newer device loses it.
        config_repo.update_section(
            _PLUGIN_ID,
            {"tasks": {"strip_ipa": {"enable": False, "a_future_option": ["kept"]}}},
        )

        _dialog(config_repo)._save()

        saved = self._saved(config_repo)["strip_ipa"]
        assert saved["a_future_option"] == ["kept"]
        # And the switch the user set is still off (the fallback did not flip it back on).
        assert saved["enable"] is False

    def test_the_known_tasks_are_still_written(self, config_repo, warnings):
        dialog = _dialog(config_repo)

        dialog._save()

        saved = self._saved(config_repo)
        for task in dialog._tasks:
            assert saved[task.task_id]["enable"] == task.is_enabled
            assert saved[task.task_id]["order"] == task.order
        assert dialog.accepted == [True]


class TestSaveFailure:
    """A write that fails must not close the dialog claiming success."""

    def test_a_backend_error_warns_and_keeps_the_dialog_open(
        self, config_repo, warnings
    ):
        class _BackendError(Exception):
            """What the collection backend raises — NOT an OSError."""

        dialog = _dialog(config_repo)

        def _explode(*_args: Any, **_kwargs: Any) -> None:
            raise _BackendError("db is locked")

        dialog._repo = types.SimpleNamespace(update_section=_explode)
        dialog._save()

        assert dialog.accepted == []
        assert len(warnings) == 1 and "db is locked" in warnings[0]
