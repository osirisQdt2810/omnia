"""Tests for the note-maintenance dialogs' non-Qt parts (real Qt isn't available headless).

Both modules bind their ``aqt.qt`` symbols at module top, so the stub below supplies the names
they import — which also makes importing them a smoke test: these files are the only ones in
the package no other test can load. What is actually asserted is the pieces with logic in
them: the diff palette, which turns ``diff.py``'s semantic ``<del>``/``<ins>`` marks into the
inline styles Qt's rich text engine understands, and the settings panel — which note types and
fields it offers (read from a fake collection), and what its save keeps, including the note
types, tasks, options and shapes this version cannot show.
"""

from __future__ import annotations

import enum
import sys
import types
from typing import Any

import pytest

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
    _ORDER_STEP,
    NoteMaintenanceSettingsDialog,
    _NoteTypeTasksEditor,
)
from omnia.gui.note_maintenance.preview_dialog import (  # noqa: E402
    _DIFF_COLUMN,
    _DiffPalette,
    _PreviewTree,
)
from omnia.plugins.note_maintenance.field_choices import FieldChoices  # noqa: E402
from omnia.plugins.note_maintenance.registry import build_tasks  # noqa: E402
from omnia.plugins.note_maintenance.settings_merge import (  # noqa: E402
    TaskOptions,
    TaskSectionMerge,
)

_PLUGIN_ID = "note_maintenance"


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
        monkeypatch.setattr(
            _qt.QTimer, "singleShot", lambda _ms, _ctx, fn: timers.append(fn)
        )
        done: list = []
        tree = self._tree(done)

        for _ in range(50):
            tree._on_section_resized(_DIFF_COLUMN, 0, 0)

        assert len(timers) == 1
        timers[0]()  # the drag settles
        assert done == ["laid out"]

    def test_a_later_drag_schedules_again(self, monkeypatch):
        timers: list = []
        monkeypatch.setattr(
            _qt.QTimer, "singleShot", lambda _ms, _ctx, fn: timers.append(fn)
        )
        tree = self._tree([])

        tree._on_section_resized(_DIFF_COLUMN, 0, 0)
        timers[0]()
        tree._on_section_resized(_DIFF_COLUMN, 0, 0)

        assert len(timers) == 2

    def test_the_label_column_never_schedules(self, monkeypatch):
        timers: list = []
        monkeypatch.setattr(
            _qt.QTimer, "singleShot", lambda _ms, _ctx, fn: timers.append(fn)
        )

        self._tree([])._on_section_resized(0, 0, 0)

        assert timers == []


class _FakeCheckItem:
    """A list row reduced to what the save reads: its tick."""

    def __init__(self, checked: bool) -> None:
        self._checked = checked

    def setChecked(self, checked: bool) -> None:
        """Stand in for the user clicking the tick."""
        self._checked = checked

    def checkState(self) -> Any:
        state = _CheckState.Checked if self._checked else _CheckState.Unchecked
        return getattr(_qt.Qt.CheckState, state.name)


class _FakeCheckList:
    """A checkable column: one :class:`_FakeCheckItem` per row, in the panel's own order."""

    def __init__(self, checked: list[bool]) -> None:
        self._items = [_FakeCheckItem(state) for state in checked]

    def item(self, row: int) -> _FakeCheckItem:
        return self._items[row]

    def count(self) -> int:
        return len(self._items)


class _FakeNoteTypeEditor:
    """Stands in for one note type's task editor: it reports the task map it would save.

    Carries ``is_untouched`` for the same reason the real editor does — the dialog builds an
    editor merely by SHOWING a note type, and that must not count as configuring it.
    """

    def __init__(self, values: dict[str, Any], seed: dict[str, Any]) -> None:
        self._values = dict(values)
        self._seed = dict(seed)

    def values(self) -> dict[str, Any]:
        return dict(self._values)

    @property
    def is_untouched(self) -> bool:
        return self._values == self._seed


class _FakeModels:
    """Anki's ``col.models``, reduced to the two calls ``anki_compat`` makes."""

    def __init__(self, note_types: dict[str, list[str]]) -> None:
        self._note_types = dict(note_types)

    def all(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "flds": [{"name": field} for field in fields]}
            for name, fields in self._note_types.items()
        ]

    def by_name(self, name: str) -> Any:
        fields = self._note_types.get(name)
        return (
            None
            if fields is None
            else {"name": name, "flds": [{"name": f} for f in fields]}
        )


@pytest.fixture
def collection(monkeypatch):
    """Point ``anki_compat`` at a fake collection; the test declares its note types."""

    def _install(note_types: dict[str, list[str]]) -> None:
        monkeypatch.setattr(
            aqt,
            "mw",
            types.SimpleNamespace(
                col=types.SimpleNamespace(models=_FakeModels(note_types))
            ),
            raising=False,
        )

    _install({"Vocab": ["Word", "Synonyms", "SynonymsNoIPA"]})
    return _install


def _untouched_editor(stored_tasks: dict[str, Any]) -> _FakeNoteTypeEditor:
    """What one note type's real editor reports when the user edits nothing.

    Mirrors ``_NoteTypeTasksEditor.values()`` exactly — the same ``TaskOptions`` split (its
    rendered rows plus what no renderer knows) merged onto the same raw stored map — so a round
    trip exercises the live save path without a Qt widget.
    """
    merge = TaskSectionMerge(stored_tasks)
    # `build_tasks` returns them in run order, which IS the list's order — so the position a
    # save writes is the row index, exactly as `_NoteTypeTasksEditor.values()` computes it.
    for row, task in enumerate(build_tasks(stored_tasks)):
        options = TaskOptions(task.config)
        rendered = {row_.name: row_.value for row_ in options.rows}
        merge.apply(
            task.task_id,
            enable=task.is_enabled,
            order=(row + 1) * _ORDER_STEP,
            options={**options.passthrough, **rendered},
        )
    return _FakeNoteTypeEditor(merge.result(), seed=merge.result())


def _dialog(repo: Any) -> NoteMaintenanceSettingsDialog:
    """The dialog with its Qt half faked out, loaded from ``repo`` exactly as on open.

    ``__new__`` skips ``QDialog.__init__`` (no Qt headless); everything :meth:`_save` touches
    is then filled in the same order — and with the same values — the real constructor does.
    """
    dialog = NoteMaintenanceSettingsDialog.__new__(NoteMaintenanceSettingsDialog)
    dialog._repo = repo
    dialog._load()
    dialog._editors = {}
    dialog._note_type_list = _FakeCheckList(
        [dialog._scope.is_enabled(name) for name in dialog._note_type_names]
    )
    dialog.accepted = []
    dialog.accept = lambda: dialog.accepted.append(True)
    return dialog


def _open(dialog: NoteMaintenanceSettingsDialog, note_type: str) -> None:
    """Select ``note_type`` — building its editor, which is what a save then reads.

    Mirrors what the real ``_on_note_type_changed`` does, including for the FIRST note type,
    which the constructor selects itself so the pane is not blank (see :func:`_look_at`).
    """
    dialog._editors[note_type] = _untouched_editor(dialog._task_sections_for(note_type))


def _configure(dialog: NoteMaintenanceSettingsDialog, note_type: str) -> None:
    """Open ``note_type`` and CHANGE something in it, as a configuring user would."""
    _open(dialog, note_type)
    editor = dialog._editors[note_type]
    dialog._editors[note_type] = _FakeNoteTypeEditor(
        {**editor.values(), "strip_ipa": {"enable": True, "fields": {"IPA": ""}}},
        seed=editor._seed,
    )


def _tick(dialog: NoteMaintenanceSettingsDialog, note_type: str, checked: bool) -> None:
    """Tick or untick ``note_type``'s row."""
    dialog._note_type_list.item(dialog._note_type_names.index(note_type)).setChecked(
        checked
    )


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


class TestNoteTypeColumn:
    """Which note types the panel offers, and which of them are ticked."""

    def test_the_collections_note_types_are_listed(self, config_repo, collection):
        collection({"Vocab": ["Word"], "Basic": ["Front", "Back"]})

        assert _dialog(config_repo)._note_type_names == ["Vocab", "Basic"]

    def test_a_configured_note_type_the_collection_lacks_is_still_listed(
        self, config_repo, collection
    ):
        # Renamed, deleted, or another device's. Listing it is what lets the save keep it —
        # and what tells the user why that note type never seems to run.
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID, {"note_types": {"Gone": {"enable": True}}}
        )

        dialog = _dialog(config_repo)

        assert dialog._note_type_names == ["Vocab", "Gone"]
        assert dialog._missing == ["Gone"]

    def test_the_ticks_follow_the_stored_enable(self, config_repo, collection):
        collection({"Vocab": ["Word"], "Basic": ["Front"]})
        config_repo.update_section(
            _PLUGIN_ID,
            {"note_types": {"Vocab": {"enable": True}, "Basic": {"enable": False}}},
        )

        dialog = _dialog(config_repo)

        assert dialog._note_type_list.item(0).checkState() == _qt.Qt.CheckState.Checked
        assert (
            dialog._note_type_list.item(1).checkState() == _qt.Qt.CheckState.Unchecked
        )


class TestFieldDropdowns:
    """Every field option offers THAT note type's real fields — and keeps a value it lost."""

    def _choices(self, dialog: Any, note_type: str, blank: str = "") -> FieldChoices:
        return FieldChoices(dialog._fields_of(note_type), blank_label=blank)

    def test_the_fields_come_from_the_collections_note_type(
        self, config_repo, collection
    ):
        collection({"Vocab": ["Word", "Synonyms"], "Basic": ["Front", "Back"]})
        dialog = _dialog(config_repo)

        assert dialog._fields_of("Vocab") == ["Word", "Synonyms"]
        assert [
            choice.value for choice in self._choices(dialog, "Basic").entries("Front")
        ] == ["Front", "Back"]

    def test_a_note_type_the_collection_does_not_have_offers_no_fields(
        self, config_repo, collection
    ):
        collection({"Vocab": ["Word"]})

        assert _dialog(config_repo)._fields_of("Gone") == []

    def test_a_stored_field_the_note_type_no_longer_has_is_kept_and_marked(
        self, config_repo, collection
    ):
        collection({"Vocab": ["Word", "Notes"]})
        dialog = _dialog(config_repo)

        entries = self._choices(dialog, "Vocab").entries("Synonyms")

        assert entries[-1].value == "Synonyms" and entries[-1].is_stale
        assert "not in this note type" in entries[-1].label

    def test_a_stale_field_value_survives_the_save(
        self, config_repo, collection, warnings
    ):
        # The dropdown marks it; the SAVE must still write it back. Replacing it with whatever
        # sits at the top of the list is the data loss this plugin already shipped three of.
        collection({"Vocab": ["Word", "Notes"]})
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        saved = _saved(config_repo)["Vocab"]["tasks"]["reformat_synonyms"]
        assert saved["field"] == "Synonyms"


class _FakeTaskList:
    """A QListWidget reduced to what `_move` does to it: take a row, insert it, select it."""

    def __init__(self, labels: list[str]) -> None:
        self.rows = list(labels)
        self._current = 0

    def currentRow(self) -> int:
        return self._current

    def setCurrentRow(self, row: int) -> None:
        self._current = row

    def takeItem(self, row: int) -> str:
        return self.rows.pop(row)

    def insertItem(self, row: int, item: str) -> None:
        self.rows.insert(row, item)


class TestTaskReordering:
    """The run order IS the task list's order, changed with ▲/▼ rather than typed."""

    @staticmethod
    def _editor() -> Any:
        """A `_NoteTypeTasksEditor` with only the two things `_move` touches filled in."""
        editor = _NoteTypeTasksEditor.__new__(_NoteTypeTasksEditor)
        editor._tasks = build_tasks({})
        editor._task_list = _FakeTaskList([t.task_id for t in editor._tasks])
        editor._move_up = types.SimpleNamespace(setEnabled=lambda _v: None)
        editor._move_down = types.SimpleNamespace(setEnabled=lambda _v: None)
        return editor

    def test_moving_a_task_up_moves_it_in_the_list_and_in_the_run(self):
        editor = self._editor()
        before = [task.task_id for task in editor._tasks]
        editor._task_list.setCurrentRow(2)

        editor._move(-1)

        expected = [*before[:1], before[2], before[1], *before[3:]]
        assert [task.task_id for task in editor._tasks] == expected
        # The list and the task sequence must move TOGETHER: `values()` pairs them by index,
        # so letting the two drift would save one task's tick against another's options.
        assert editor._task_list.rows == expected
        assert editor._task_list.currentRow() == 1  # the moved task stays highlighted

    def test_moving_down_is_the_mirror(self):
        editor = self._editor()
        before = [task.task_id for task in editor._tasks]
        editor._task_list.setCurrentRow(0)

        editor._move(1)

        assert [task.task_id for task in editor._tasks] == [
            before[1],
            before[0],
            *before[2:],
        ]
        assert editor._task_list.currentRow() == 1

    def test_moving_off_either_end_does_nothing(self):
        editor = self._editor()
        before = list(editor._task_list.rows)

        editor._task_list.setCurrentRow(0)
        editor._move(-1)
        editor._task_list.setCurrentRow(len(before) - 1)
        editor._move(1)

        assert editor._task_list.rows == before


class TestMultipleNoteTypes:
    """Several note types are configured and maintained together, each keeping its own."""

    def test_each_ticked_note_type_is_saved_with_its_own_tasks(
        self, config_repo, collection, warnings
    ):
        collection({"Vocab": ["Word", "Synonyms"], "Kanji": ["Reading"]})
        dialog = _dialog(config_repo)
        for name in ("Vocab", "Kanji"):
            _tick(dialog, name, True)
            _open(dialog, name)

        dialog._save()

        saved = _saved(config_repo)
        assert set(saved) == {"Vocab", "Kanji"}
        assert saved["Vocab"]["enable"] is True and saved["Kanji"]["enable"] is True
        assert dialog.accepted == [True]

    def test_the_saved_order_is_the_list_position_not_a_typed_number(
        self, config_repo, collection, warnings
    ):
        # `order` is no longer an option anyone types: it IS the task list's ▲/▼ order, so a
        # save stamps consecutive positions down the list. It stays PERSISTED because the
        # runner sorts on it and an older Omnia reads it.
        collection({"Vocab": ["Word", "Synonyms"]})
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        # Keyed by task, not by dict iteration order — storage does not promise to keep the
        # map's insertion order, and the run order lives in the numbers, not in the keys.
        saved = _saved(config_repo)["Vocab"]["tasks"]
        expected = {
            task.task_id: (row + 1) * _ORDER_STEP
            for row, task in enumerate(build_tasks({}))
        }

        assert {name: section["order"] for name, section in saved.items()} == expected

    def test_a_note_type_the_user_never_touched_gets_no_entry(
        self, config_repo, collection, warnings
    ):
        # The config records real choices, not a copy of the collection's note types.
        collection({"Vocab": ["Word"], "Basic": ["Front"]})
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        assert set(_saved(config_repo)) == {"Vocab"}

    def test_merely_showing_a_note_type_is_not_configuring_it(
        self, config_repo, collection, warnings
    ):
        # The constructor selects the FIRST note type so the right-hand pane is not blank,
        # which builds its editor without the user choosing anything — and the same happens for
        # every note type they click past. Saving then wrote an entry for it, seeded from the
        # legacy global map, so ticking it later silently inherited field names written for a
        # DIFFERENT note type. Being looked at is not being configured.
        collection({"Basic": ["Front"], "Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID, {"tasks": {"replace_text_all_fields": {"find": "PROMO"}}}
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Basic")  # what setCurrentRow(0) does on open
        _open(dialog, "Vocab")  # …and clicking through to the next one

        dialog._save()

        assert _saved(config_repo) == {}

    def test_ticking_a_note_type_still_carries_the_legacy_map_over(
        self, config_repo, collection, warnings
    ):
        # The other half of the rule: an untouched editor IS saved when its note type is
        # ticked, because that untouched map is the legacy seed — ticking is how an upgrade
        # keeps what the user had configured globally.
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID, {"tasks": {"replace_text_all_fields": {"find": "PROMO"}}}
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")
        _tick(dialog, "Vocab", True)

        dialog._save()

        saved = _saved(config_repo)["Vocab"]
        assert saved["enable"] is True
        assert saved["tasks"]["replace_text_all_fields"]["find"] == "PROMO"

    def test_configuring_a_note_type_saves_it_even_unticked(
        self, config_repo, collection, warnings
    ):
        # Changing settings without ticking is a real edit — it must survive, so the work is
        # still there when the note type is switched on later.
        collection({"Vocab": ["Word"]})
        dialog = _dialog(config_repo)
        _configure(dialog, "Vocab")

        dialog._save()

        saved = _saved(config_repo)["Vocab"]
        assert saved["enable"] is False
        assert saved["tasks"]["strip_ipa"]["fields"] == {"IPA": ""}

    def test_unticking_a_note_type_keeps_its_settings(
        self, config_repo, collection, warnings
    ):
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID,
            {
                "note_types": {
                    "Vocab": {"enable": True, "tasks": {"strip_ipa": {"order": 7}}}
                }
            },
        )
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", False)

        dialog._save()

        saved = _saved(config_repo)["Vocab"]
        assert saved["enable"] is False
        assert saved["tasks"] == {"strip_ipa": {"order": 7}}

    def test_a_note_type_is_seeded_from_the_pre_note_type_task_map(
        self, config_repo, collection, warnings
    ):
        # An upgrade must not throw away what the user had configured globally: the legacy map
        # is the starting point for a note type that has none of its own.
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID, {"tasks": {"replace_text_all_fields": {"find": "PROMO"}}}
        )
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        saved = _saved(config_repo)["Vocab"]["tasks"]
        assert saved["replace_text_all_fields"]["find"] == "PROMO"


def _saved(repo: Any) -> dict[str, Any]:
    """The stored ``note_types`` map, read RAW — what actually reached storage."""
    return repo.raw_section(_PLUGIN_ID).get("note_types", {})


class TestSaveKeepsWhatItCannotShow:
    """``update_section`` replaces the whole map, so the save must carry it all.

    Rebuilding that map from what this build knows — its registered tasks, this collection's
    note types, the options its form can draw — deletes everything else: ADR-010's data
    shredder one layer up, where the model-level fix cannot reach.
    """

    def test_a_note_type_this_collection_does_not_have_survives(
        self, config_repo, collection, warnings
    ):
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID,
            {"note_types": {"Gone": {"enable": True, "tasks": {"strip_ipa": {}}}}},
        )
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        assert _saved(config_repo)["Gone"] == {
            "enable": True,
            "tasks": {"strip_ipa": {}},
        }

    def test_a_task_section_this_build_never_heard_of_survives(
        self, config_repo, collection, warnings
    ):
        config_repo.update_section(
            _PLUGIN_ID,
            {
                "note_types": {
                    "Vocab": {
                        "enable": True,
                        "tasks": {"a_future_task": {"enable": True, "mode": "beta"}},
                    }
                }
            },
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")

        dialog._save()

        assert _saved(config_repo)["Vocab"]["tasks"]["a_future_task"] == {
            "enable": True,
            "mode": "beta",
        }

    def test_an_option_key_from_a_newer_omnia_survives(
        self, config_repo, collection, warnings
    ):
        # End of the ADR-010 chain: the task's model keeps the key, the form carries it as a
        # passthrough, and the save has to put it back — or the older device deletes the newer
        # one's option on the next sync.
        config_repo.update_section(
            _PLUGIN_ID,
            {
                "note_types": {
                    "Vocab": {
                        "enable": True,
                        "tasks": {
                            "strip_ipa": {"enable": False, "a_future_option": ["kept"]}
                        },
                    }
                }
            },
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")

        dialog._save()

        saved = _saved(config_repo)["Vocab"]["tasks"]["strip_ipa"]
        assert saved["a_future_option"] == ["kept"]
        # And the switch the user set is still off (the fallback did not flip it back on).
        assert saved["enable"] is False

    def test_a_readable_option_is_not_reverted_by_an_unreadable_sibling(
        self, config_repo, collection, warnings
    ):
        # The save persists what the dialog SHOWS, so an option the per-task fallback dropped
        # would be written back as the shipped default: the user's find text, destroyed by a
        # garbage ``order`` next to it.
        config_repo.update_section(
            _PLUGIN_ID,
            {
                "note_types": {
                    "Vocab": {
                        "enable": True,
                        "tasks": {
                            "replace_text_all_fields": {
                                "enable": True,
                                "order": "whenever",
                                "find": "PROMO",
                                "a_future_option": ["kept"],
                            }
                        },
                    }
                }
            },
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")

        dialog._save()

        saved = _saved(config_repo)["Vocab"]["tasks"]["replace_text_all_fields"]
        assert saved["find"] == "PROMO"
        assert saved["enable"] is True
        assert saved["a_future_option"] == ["kept"]

    def test_a_task_entry_that_is_not_a_table_survives(
        self, config_repo, collection, warnings
    ):
        # A type error inside ``tasks`` is the other way the typed read raises, and one that
        # tolerating unknown KEYS cannot rescue.
        config_repo.update_section(
            _PLUGIN_ID,
            {"note_types": {"Vocab": {"enable": True, "tasks": {"weird": "a string"}}}},
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")

        dialog._save()

        assert _saved(config_repo)["Vocab"]["tasks"]["weird"] == "a string"

    def test_a_note_type_entry_that_is_not_a_table_survives(
        self, config_repo, collection, warnings
    ):
        config_repo.update_section(_PLUGIN_ID, {"note_types": {"Weird": "a string"}})
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        assert _saved(config_repo)["Weird"] == "a string"

    def test_a_section_level_key_this_build_never_heard_of_keeps_the_map(
        self, config_repo, collection, warnings
    ):
        # The pass-through must not depend on the ``[note_maintenance]`` section PARSING: one
        # key from a newer Omnia beside ``note_types`` used to make the typed read raise, and
        # the save then wrote this build's world over everything stored.
        config_repo.update_section(
            _PLUGIN_ID,
            {"note_types": {"Gone": {"enable": True}}, "some_new_key": 1},
        )
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        assert _saved(config_repo)["Gone"] == {"enable": True}

    def test_the_pre_note_type_task_map_is_left_exactly_as_stored(
        self, config_repo, collection, warnings
    ):
        # An OLDER Omnia still runs that map; this build must not rewrite or delete it.
        config_repo.update_section(
            _PLUGIN_ID, {"tasks": {"strip_ipa": {"enable": True, "order": 20}}}
        )
        dialog = _dialog(config_repo)
        _tick(dialog, "Vocab", True)
        _open(dialog, "Vocab")

        dialog._save()

        assert config_repo.raw_section(_PLUGIN_ID)["tasks"] == {
            "strip_ipa": {"enable": True, "order": 20}
        }

    def test_a_note_type_configured_under_another_case_is_not_forked(
        self, config_repo, collection, warnings
    ):
        collection({"Vocab": ["Word"]})
        config_repo.update_section(
            _PLUGIN_ID, {"note_types": {"vocab": {"enable": True, "tasks": {}}}}
        )
        dialog = _dialog(config_repo)
        _open(dialog, "Vocab")

        dialog._save()

        assert list(_saved(config_repo)) == ["vocab"]


class TestSaveFailure:
    """A write that fails must not close the dialog claiming success."""

    def test_a_backend_error_warns_and_keeps_the_dialog_open(
        self, config_repo, collection, warnings
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
