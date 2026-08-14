"""Bespoke settings dialog for Note Maintenance: which note types, which tasks, and how.

The generic settings form renders a plugin's FLAT options, but this plugin's settings are a map
of maps of maps — ``{note type: {tasks: {task id: {enable, order, …that task's options}}}}`` —
and the options that matter name FIELDS, which only exist inside one note type. So the plugin
declares this dialog instead (the same reason Word Lookup has one).

Shape: note types down the left (ticked = maintained; tick as many as you like), and for
whichever one is highlighted, its tasks and the highlighted task's options — with every field
option a dropdown of THAT note type's real fields, so a field name is picked, never typed.

Two things this dialog must never do, both of which it has done before (CONVENTIONS Part 2):

* rebuild a stored map from what this build knows. It reads the RAW section and merges onto it
  (:mod:`~omnia.plugins.note_maintenance.settings_merge`), so a note type this collection does
  not have, a task a newer Omnia added, an option no renderer knows and an entry that is not a
  table all survive a save;
* drop a field value the note type no longer has. It is kept and shown as a stale entry
  (:class:`~omnia.plugins.note_maintenance.field_choices.FieldChoices`) — a renamed field must
  not silently rewrite the user's setting to whatever lands at the top of a dropdown.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, cast

from aqt.qt import (  # type: ignore[attr-defined]
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    Qt,
    QVBoxLayout,
    QWidget,
)

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import ConfigField
from omnia.gui.config_form import ConfigFieldEditor
from omnia.gui.note_maintenance.field_editors import FieldMapEditor, NoteFieldCombo
from omnia.gui.widgets import hint_label
from omnia.plugins.note_maintenance.base import MaintenanceTask, OptionKind
from omnia.plugins.note_maintenance.field_choices import FieldChoices
from omnia.plugins.note_maintenance.note_types import NoteTypeScope
from omnia.plugins.note_maintenance.registry import build_tasks
from omnia.plugins.note_maintenance.settings_merge import (
    NoteTypeSectionMerge,
    OptionRow,
    TaskOptions,
    TaskSectionMerge,
)

logger = get_logger("note_maintenance")

_PLUGIN_ID = "note_maintenance"
#: Marks a configured note type this collection does not have (renamed, deleted, or another
#: device's). Listed rather than dropped: its settings are the user's, not this build's to bin.
_MISSING_SUFFIX = " — not in this collection"


class _TaskOptionsEditor(QWidget):
    """The options form for ONE task of ONE note type, built from that task's settings model.

    ``enable`` is left out — the task list's tick owns it — and every other option is rendered
    by what it HOLDS (:class:`~omnia.plugins.note_maintenance.base.OptionKind`): a plain value
    through the SAME :class:`~omnia.gui.config_form.ConfigFieldEditor` the generic per-feature
    form uses (so a kind renders identically wherever it appears), a field name as a dropdown
    of this note type's fields, and a ``{source: target}`` map as two columns of them. What no
    renderer knows is kept by
    :class:`~omnia.plugins.note_maintenance.settings_merge.TaskOptions` and written back
    unchanged.
    """

    def __init__(
        self,
        task: MaintenanceTask,
        fields: Sequence[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the form for ``task``'s current configuration.

        Args:
            task: The configured task instance (its ``config`` supplies the current values).
            fields: The field names of the note type being configured.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._options = TaskOptions(task.config)
        self._readers: dict[str, Callable[[], Any]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(f"<b>{task.name or task.task_id}</b>"))
        if task.description:
            layout.addWidget(hint_label(self, task.description))

        form = QFormLayout()
        form.setSpacing(10)
        for row in self._options.rows:
            control, read = self._control_for(row, fields)
            self._readers[row.name] = read
            form.addRow(self._label_for(row), control)
        layout.addLayout(form)
        layout.addStretch(1)

    def values(self) -> dict[str, Any]:
        """Return the options to save (without ``enable``), keyed as the config file stores them.

        The edited controls plus the options this form could not render, unchanged — so a save
        never drops a setting just because the dialog did not know how to show it.
        """
        edited = {name: read() for name, read in self._readers.items()}
        return {**self._options.passthrough, **edited}

    @staticmethod
    def _control_for(
        row: OptionRow, fields: Sequence[str]
    ) -> tuple[QWidget, Callable[[], Any]]:
        """Return the control for one option, and the callable that reads its value back."""
        if row.kind is OptionKind.FIELD_MAP:
            mapping = FieldMapEditor(
                fields, row.value if isinstance(row.value, dict) else {}
            )
            return mapping, mapping.values
        if row.kind is OptionKind.NOTE_FIELD:
            combo = NoteFieldCombo(FieldChoices(fields), str(row.value or ""))
            return combo, combo.value
        # Only a field map has no descriptor, and it returned above.
        field = cast(ConfigField, row.field)
        editor = ConfigFieldEditor(field, row.value)
        # The generic form puts the help behind an (i) button it lays out itself; this one is a
        # multi-column panel, so the help rides on the control as a tooltip instead.
        editor.widget.setToolTip(field.help)
        return editor.widget, editor.value

    def _label_for(self, row: OptionRow) -> QLabel:
        """The row label: the scalar descriptor's, or the model's own for a field map."""
        if row.field is not None:
            label = QLabel(row.field.label)
            label.setToolTip(row.field.help)
            return label
        info = self._options.model.__fields__[row.name].field_info
        label = QLabel(info.title or row.name.replace("_", " ").capitalize())
        label.setToolTip(info.description or "")
        return label


class _NoteTypeTasksEditor(QWidget):
    """One note type's whole task set: the task list, and the highlighted task's options.

    Owns the merge for its own ``tasks`` map, so the dialog above only has to ask each note
    type it opened what to save.
    """

    def __init__(
        self,
        stored_tasks: Mapping[str, Any],
        fields: Sequence[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the editor.

        Args:
            stored_tasks: The note type's RAW ``{task id: options}`` map (what a save merges
                onto, so an entry this build cannot read survives).
            fields: The note type's field names, for the field dropdowns.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._stored_tasks = dict(stored_tasks)
        self._tasks = build_tasks(self._stored_tasks)
        self._editors: dict[str, _TaskOptionsEditor] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._task_column(), 1)
        layout.addWidget(self._options_column(fields), 2)
        if self._task_list.count():
            self._task_list.setCurrentRow(0)

    def values(self) -> dict[str, Any]:
        """Return the ``tasks`` map to persist: what was stored, updated with what was edited."""
        merge = TaskSectionMerge(self._stored_tasks)
        for row, task in enumerate(self._tasks):
            item = self._task_list.item(row)
            merge.apply(
                task.task_id,
                enable=item.checkState() == Qt.CheckState.Checked,
                options=self._editors[task.task_id].values(),
            )
        return merge.result()

    @property
    def is_untouched(self) -> bool:
        """Whether this editor still holds exactly what it was opened with.

        The dialog auto-selects the first note type so the right-hand pane is not blank on
        open, which BUILDS that note type's editor without the user having chosen anything.
        Without this, saving an untouched dialog wrote an entry for whichever note type
        happened to sort first — seeded from the legacy global task map, so ticking it later
        would silently inherit field names written for a different note type. Merely being
        looked at is not configuring.
        """
        return self.values() == self._stored_tasks

    def _task_column(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Tasks</b>"))

        self._task_list = QListWidget()
        self._task_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for task in self._tasks:
            item = QListWidgetItem(task.name or task.task_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if task.is_enabled else Qt.CheckState.Unchecked
            )
            self._task_list.addItem(item)
        self._task_list.currentRowChanged.connect(self._on_task_changed)
        layout.addWidget(self._task_list, 1)
        layout.addWidget(
            hint_label(self, "Unticked tasks are skipped for this note type.")
        )
        return holder

    def _options_column(self, fields: Sequence[str]) -> QWidget:
        self._options = QStackedWidget()
        for task in self._tasks:
            editor = _TaskOptionsEditor(task, fields)
            self._editors[task.task_id] = editor
            self._options.addWidget(editor)
        return self._options

    def _on_task_changed(self, row: int) -> None:
        """Show the highlighted task's options (each editor keeps its own edits)."""
        if 0 <= row < self._options.count():
            self._options.setCurrentIndex(row)


class NoteMaintenanceSettingsDialog(QDialog):
    """Pick the note types a run maintains and, per note type, its tasks and their options."""

    def __init__(self, repo: Any, parent: Optional[QWidget] = None) -> None:
        """Build the dialog from the plugin's saved settings.

        Args:
            repo: The ``ConfigRepository`` (read on open, written on Save).
            parent: Parent widget.
        """
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Note Maintenance — settings")
        self.resize(980, 600)

        self._load()
        self._editors: dict[str, _NoteTypeTasksEditor] = {}

        root = QVBoxLayout(self)
        root.addWidget(
            hint_label(
                self,
                "Tick every note type a maintenance run should cover — each keeps its own "
                "tasks and its own fields, and one run maintains them all. Select a note type "
                "to configure it.",
            )
        )

        columns = QHBoxLayout()
        columns.addWidget(self._note_type_column(), 1)
        self._note_type_editors = QStackedWidget()
        columns.addWidget(self._note_type_editors, 3)
        root.addLayout(columns, 1)

        root.addWidget(
            hint_label(
                self,
                "Run it from the Browser: select the notes, right-click → "
                "🧹 Omnia · Maintain Notes… — you review every change before anything is written.",
            )
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._note_type_list.count():
            self._note_type_list.setCurrentRow(0)

    # -- construction helpers -------------------------------------------------------------

    def _load(self) -> None:
        """Read the RAW ``[note_maintenance]`` section and index what the dialog edits.

        Read raw rather than through ``feature_settings`` because the typed read is
        all-or-nothing: ONE unreadable value anywhere in the section (a key a newer Omnia
        added, an entry that is not a table) makes it raise, and a save that had started from
        an empty map would then write this build's own world over everything the user had.
        """
        section = self._repo.raw_section(_PLUGIN_ID)
        self._stored_note_types = _table(section.get("note_types"))
        # The pre-note-type global map. Never rewritten (an older Omnia still runs it) and
        # never run here — only offered as the starting point for a note type that has no
        # tasks of its own, so an upgrade does not throw away what the user had configured.
        self._legacy_tasks = _table(section.get("tasks"))
        self._scope = NoteTypeScope(self._stored_note_types)
        collection_names = self._all_note_types()
        known = {name.strip().lower() for name in collection_names}
        self._missing = [
            name for name in self._scope.names if name.strip().lower() not in known
        ]
        self._note_type_names = list(collection_names) + self._missing

    def _note_type_column(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Note types</b>"))

        self._note_type_list = QListWidget()
        self._note_type_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        missing = set(self._missing)
        for name in self._note_type_names:
            item = QListWidgetItem(
                f"{name}{_MISSING_SUFFIX}" if name in missing else name
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if self._scope.is_enabled(name)
                else Qt.CheckState.Unchecked
            )
            self._note_type_list.addItem(item)
        self._note_type_list.currentRowChanged.connect(self._on_note_type_changed)
        layout.addWidget(self._note_type_list, 1)
        layout.addWidget(
            hint_label(
                self,
                "Unticked note types are left alone — their settings are kept, not deleted.",
            )
        )
        return holder

    def _on_note_type_changed(self, row: int) -> None:
        """Show the highlighted note type's tasks, building its editor the first time."""
        if not 0 <= row < len(self._note_type_names):
            return
        name = self._note_type_names[row]
        editor = self._editors.get(name)
        if editor is None:
            editor = _NoteTypeTasksEditor(
                self._task_sections_for(name), self._fields_of(name)
            )
            self._editors[name] = editor
            self._note_type_editors.addWidget(editor)
        self._note_type_editors.setCurrentWidget(editor)

    def _task_sections_for(self, note_type: str) -> dict[str, Any]:
        """The task map to show for ``note_type``: its own, or the legacy global one as a seed."""
        stored = self._scope.task_sections(note_type)
        return stored if stored else dict(self._legacy_tasks)

    @staticmethod
    def _all_note_types() -> list[str]:
        """Every note type name in the collection (empty on any failure — never crash setup)."""
        try:
            return list(anki_compat.note_type_names())
        except Exception:
            logger.exception("note_maintenance: could not read note type names")
            return []

    @staticmethod
    def _fields_of(note_type: str) -> list[str]:
        """``note_type``'s field names, from the collection (empty when it has none here)."""
        try:
            return list(anki_compat.note_type_field_names(note_type))
        except Exception:
            logger.exception("note_maintenance: could not read fields of %r", note_type)
            return []

    # -- persistence ----------------------------------------------------------------------

    def _save(self) -> None:
        """Write the note-type map back into the plugin's config section.

        A write that fails leaves the dialog OPEN with a warning: closing it as if the settings
        had been saved loses the user's edits AND tells them a lie they only discover the next
        time they open the panel.
        """
        from aqt.utils import showWarning

        try:
            self._repo.update_section(
                _PLUGIN_ID, {"note_types": self._note_types_to_save()}
            )
        # Broad by necessity, at the UI boundary: what a failed write raises depends on the
        # storage backend — an anki backend error from ``col.set_config`` (the default,
        # collection-backed one), OSError/TypeError from the TOML writer, or a ValidationError
        # from the reload that follows. Narrowing to one of them silently swallows the others
        # and reports the save as successful.
        except Exception as exc:
            logger.exception("note_maintenance: could not save settings")
            showWarning(
                f"Omnia: could not save the Note Maintenance settings.\n\n{exc}"
            )
            return
        self.accept()

    def _note_types_to_save(self) -> dict[str, Any]:
        """The whole ``note_types`` map to persist: what was stored, updated with what changed.

        Only note types the user actually touched are written: a ticked one, one whose tasks
        they CHANGED, or one that already holds readable settings (whose tick may just have
        been cleared). Everything else is left exactly as stored — so a never-configured note
        type gets no entry at all, and an entry this build could not read as settings is not
        overwritten by a tick the user never set.

        "Changed", not "opened": the dialog builds the first note type's editor itself so the
        pane is not blank on open (and builds one for every note type the user clicks through),
        and none of that is a decision — see :attr:`_NoteTypeTasksEditor.is_untouched`.

        A note type that IS ticked still saves its editor's whole map even when untouched, and
        that is the point: for a note type with no settings of its own the editor was seeded
        from the legacy global map, so ticking it is what carries an upgrade's existing
        configuration over.
        """
        merge = NoteTypeSectionMerge(self._stored_note_types)
        for row, name in enumerate(self._note_type_names):
            ticked = (
                self._note_type_list.item(row).checkState() == Qt.CheckState.Checked
            )
            editor = self._editors.get(name)
            touched = editor is not None and not editor.is_untouched
            if not ticked and not touched and not self._scope.is_configured(name):
                continue
            stored_name = self._scope.stored_name(name)
            merge.apply(
                # Write under the STORED spelling where there is one, so ticking a note type
                # cannot fork its settings into a second entry differing only in case.
                stored_name or name,
                enable=ticked,
                tasks=editor.values() if editor is not None else None,
            )
        return merge.result()


def _table(value: Any) -> dict[str, Any]:
    """Return ``value`` as a dict, or ``{}`` when it is absent or is not a table."""
    return dict(value) if isinstance(value, dict) else {}
