"""Bespoke settings dialog for Note Maintenance: which tasks run, in what order, and how.

The generic settings form renders a plugin's FLAT options, but this plugin's settings are a
map of maps — ``{task id: {enable, order, …that task's own options}}`` — and two of the bundled
tasks are configured by a ``{source field: target field}`` mapping the flat form cannot express
at all. So the plugin declares this dialog instead (the same reason Word Lookup has one).

Shape: the tasks down the left (ticked = takes part in a run), and for whichever one is
highlighted, its options on the right — derived from that task's own Pydantic model, so a new
task shows up here with its options and help text without this dialog being edited.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Optional

from aqt.qt import (  # type: ignore[attr-defined]
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pydantic import BaseModel, ValidationError

from omnia.core.config.schema import schema_from_model
from omnia.core.logging import get_logger
from omnia.core.plugin import ConfigField
from omnia.gui.config_form import ConfigFieldEditor
from omnia.gui.widgets import hint_label
from omnia.plugins.note_maintenance.base import MaintenanceTask
from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings
from omnia.plugins.note_maintenance.registry import build_tasks

logger = get_logger("note_maintenance")

_PLUGIN_ID = "note_maintenance"


class _FieldMapEditor(QWidget):
    """Edits a ``{source field: target field}`` option as a two-column table.

    This is the option the generic form cannot render, and the reason the tasks that own one
    (strip IPA, extract audio file name) need a bespoke panel: each row says "read THIS field,
    write the result to THAT one", and an empty target means "rewrite the source in place".
    """

    def __init__(
        self, values: Mapping[str, str], parent: Optional[QWidget] = None
    ) -> None:
        """Build the table.

        Args:
            values: The current ``{source: target}`` mapping.
            parent: Parent widget.
        """
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(
            ["Read field", "Write to (empty = in place)"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)
        for source, target in values.items():
            self._add_row(source, target)

        buttons = QHBoxLayout()
        add = QPushButton("Add field")
        add.clicked.connect(lambda: self._add_row("", ""))
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def values(self) -> dict[str, str]:
        """Return the edited mapping (rows with no source field are dropped)."""
        mapping: dict[str, str] = {}
        for row in range(self._table.rowCount()):
            source = self._cell(row, 0)
            if source:
                mapping[source] = self._cell(row, 1)
        return mapping

    def _add_row(self, source: str, target: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(source))
        self._table.setItem(row, 1, QTableWidgetItem(target))

    def _remove_selected(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self._table.item(row, column)
        return item.text().strip() if item is not None else ""


class _TaskOptions:
    """Splits ONE task's saved options into the rows this form renders and the rest.

    Pure (no Qt), because the interesting half is what the form CANNOT render: a list-shaped
    option, or a key a newer Omnia wrote and this version has never heard of. Those are carried
    through the save untouched — a settings dialog that writes back only what it could draw
    deletes the settings it did not understand (the ADR-010 hazard, one layer up).

    ``enable`` is dropped from both halves: the task list's tick owns it and
    :meth:`NoteMaintenanceSettingsDialog._save` writes it back itself.

    Attributes:
        model: The task's settings model (for a complex row's title/help).
        rows: ``(name, value, field)`` in model order — ``field`` is the scalar descriptor to
            render with, or None for a field mapping (a :class:`_FieldMapEditor` row).
        passthrough: The options with no renderer, kept verbatim for the save.
    """

    def __init__(self, config: BaseModel) -> None:
        """Classify ``config``'s options.

        Args:
            config: The task's parsed settings (an instance, so it carries current values).
        """
        self.model = type(config)
        scalars = {field.key: field for field in schema_from_model(self.model)}
        self.rows: list[tuple[str, Any, Optional[ConfigField]]] = []
        self.passthrough: dict[str, Any] = {}
        for name, value in config.dict().items():
            if name == "enable":
                continue
            if isinstance(value, dict):
                self.rows.append((name, value, None))
            elif name in scalars:
                self.rows.append((name, value, scalars[name]))
            else:
                self.passthrough[name] = value


class _TaskOptionsEditor(QWidget):
    """The options form for ONE task, built from that task's own settings model.

    ``enable`` is left out — the task list's tick owns it — and every other option is rendered
    by kind: scalars through the SAME :class:`~omnia.gui.config_form.ConfigFieldEditor` the
    generic per-feature form uses (so a kind renders identically wherever it appears), and the
    field mappings that deriver skips as a :class:`_FieldMapEditor`. What no renderer knows is
    kept by :class:`_TaskOptions` and written back unchanged.
    """

    def __init__(self, task: MaintenanceTask, parent: Optional[QWidget] = None) -> None:
        """Build the form for ``task``'s current configuration.

        Args:
            task: The configured task instance (its ``config`` supplies the current values).
            parent: Parent widget.
        """
        super().__init__(parent)
        self._options = _TaskOptions(task.config)
        self._readers: dict[str, Callable[[], Any]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(f"<b>{task.name or task.task_id}</b>"))
        if task.description:
            layout.addWidget(hint_label(self, task.description))

        form = QFormLayout()
        form.setSpacing(10)
        for name, value, field in self._options.rows:
            if field is None:
                mapping = _FieldMapEditor(value)
                self._readers[name] = mapping.values
                form.addRow(_field_label(self._options.model, name), mapping)
                continue
            editor = ConfigFieldEditor(field, value)
            # The generic form puts the help behind an (i) button it lays out itself; this one
            # is a two-column panel, so the help rides on the control as a tooltip instead.
            editor.widget.setToolTip(field.help)
            self._readers[field.key] = editor.value
            form.addRow(_labelled(field), editor.widget)
        layout.addLayout(form)
        layout.addStretch(1)

    def values(self) -> dict[str, Any]:
        """Return the options to save (without ``enable``), keyed as the config file stores them.

        The edited controls plus the options this form could not render, unchanged — so a save
        never drops a setting just because the dialog did not know how to show it.
        """
        edited = {name: read() for name, read in self._readers.items()}
        return {**self._options.passthrough, **edited}


class NoteMaintenanceSettingsDialog(QDialog):
    """Pick the tasks a maintenance run includes, their order, and each task's options."""

    def __init__(self, repo: Any, parent: Optional[QWidget] = None) -> None:
        """Build the dialog from the plugin's saved settings.

        Args:
            repo: The ``ConfigRepository`` (read on open, written on Save).
            parent: Parent widget.
        """
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Note Maintenance — settings")
        self.resize(820, 560)

        self._saved_tasks: dict[str, dict[str, Any]] = {}
        self._tasks = self._configured_tasks()
        self._editors: dict[str, _TaskOptionsEditor] = {}

        root = QVBoxLayout(self)
        root.addWidget(
            hint_label(
                self,
                "Tick the tasks a maintenance run should include. Run order decides who goes "
                "first — two tasks touching the same field layer in that order.",
            )
        )

        columns = QHBoxLayout()
        columns.addWidget(self._task_column(), 1)
        columns.addWidget(self._options_column(), 2)
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

        if self._task_list.count():
            self._task_list.setCurrentRow(0)

    # -- construction helpers -------------------------------------------------------------

    def _configured_tasks(self) -> list[MaintenanceTask]:
        """The registered tasks carrying the user's settings (defaults if those don't parse).

        A saved value the model rejects must not lock the user out of the very dialog that would
        fix it. Per TASK that fallback lives in :func:`build_tasks` (the one gate every caller
        goes through); what is caught here is the ``[note_maintenance]`` section as a whole
        failing to parse, which happens one layer up and only on this read.

        The RAW map is kept as :attr:`_saved_tasks` for :meth:`_save` to merge onto. The tasks
        returned here only cover what THIS build registers, and each carries only what its model
        accepted, while ``update_section`` replaces the whole ``tasks`` map in one shallow
        update — so saving the tasks alone would delete a section (or an option inside one) a
        newer Omnia wrote and synced down. Same hazard as ADR-010, one layer up.
        """
        try:
            settings = self._repo.feature_settings(_PLUGIN_ID)
        except ValidationError:
            logger.exception("note_maintenance: invalid settings; showing the defaults")
            settings = None
        saved = (settings or NoteMaintenanceSettings()).tasks
        self._saved_tasks = {task_id: dict(values) for task_id, values in saved.items()}
        return build_tasks(saved)

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
        layout.addWidget(hint_label(self, "Unticked tasks are skipped by every run."))
        return holder

    def _options_column(self) -> QWidget:
        self._options = QStackedWidget()
        for task in self._tasks:
            editor = _TaskOptionsEditor(task)
            self._editors[task.task_id] = editor
            self._options.addWidget(editor)
        return self._options

    def _on_task_changed(self, row: int) -> None:
        """Show the highlighted task's options (each editor keeps its own edits)."""
        if 0 <= row < self._options.count():
            self._options.setCurrentIndex(row)

    # -- persistence ----------------------------------------------------------------------

    def _save(self) -> None:
        """Write every task's switch, order and options back into the plugin's config section.

        A write that fails leaves the dialog OPEN with a warning: closing it as if the settings
        had been saved loses the user's edits AND tells them a lie they only discover the next
        time they open the panel.
        """
        from aqt.utils import showWarning

        try:
            self._repo.update_section(_PLUGIN_ID, {"tasks": self._tasks_to_save()})
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

    def _tasks_to_save(self) -> dict[str, dict[str, Any]]:
        """The whole ``tasks`` map to persist: what was stored, updated with what was edited.

        Built on top of :attr:`_saved_tasks` (never from the registry alone) because
        ``update_section`` merges shallowly — whatever this returns IS the stored map
        afterwards. Two things therefore survive a save by this build:

        * a ``[note_maintenance.tasks.<id>]`` section for a task it does not register (a newer
          Omnia's task, synced down), and
        * an option inside a KNOWN task that its model could not read — including the case
          where that made the whole section fall back to defaults.
        """
        tasks = {task_id: dict(values) for task_id, values in self._saved_tasks.items()}
        for row, task in enumerate(self._tasks):
            item = self._task_list.item(row)
            tasks[task.task_id] = {
                **tasks.get(task.task_id, {}),
                "enable": item.checkState() == Qt.CheckState.Checked,
                # The editor supplies ``order`` too (it is a scalar option of every task
                # model), plus whatever it could not render, unchanged.
                **self._editors[task.task_id].values(),
            }
        return tasks


def _labelled(field: ConfigField) -> QLabel:
    """The row label for a scalar option (its help is on the control as a tooltip)."""
    label = QLabel(field.label)
    label.setToolTip(field.help)
    return label


def _field_label(model: type[BaseModel], name: str) -> QLabel:
    """The row label for a COMPLEX option, whose title/help the scalar deriver never sees."""
    info = model.__fields__[name].field_info
    label = QLabel(info.title or name.replace("_", " ").capitalize())
    label.setToolTip(info.description or "")
    return label
