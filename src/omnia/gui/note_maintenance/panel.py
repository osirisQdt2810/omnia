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
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPalette,
    QPushButton,
    QSpinBox,
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


class _TaskOptionsEditor(QWidget):
    """The options form for ONE task, built from that task's own settings model.

    ``enable`` is left out — the task list's tick owns it — and every other option is rendered
    by kind: scalars from the shared :func:`schema_from_model` descriptors (label, help, bounds
    and all), and the field mappings the deriver skips as a :class:`_FieldMapEditor`.
    """

    def __init__(self, task: MaintenanceTask, parent: Optional[QWidget] = None) -> None:
        """Build the form for ``task``'s current configuration.

        Args:
            task: The configured task instance (its ``config`` supplies the current values).
            parent: Parent widget.
        """
        super().__init__(parent)
        model = type(task.config)
        values = task.config.dict()
        scalars = {field.key: field for field in schema_from_model(model)}
        self._readers: dict[str, Callable[[], Any]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(f"<b>{task.name or task.task_id}</b>"))
        if task.description:
            layout.addWidget(_hint(self, task.description))

        form = QFormLayout()
        form.setSpacing(10)
        for name, value in values.items():
            if name == "enable":
                continue
            if isinstance(value, dict):
                editor = _FieldMapEditor(value)
                self._readers[name] = editor.values
                form.addRow(_field_label(model, name), editor)
                continue
            field = scalars.get(name)
            if field is None:
                continue  # an option no renderer knows — left to the config file
            widget = self._widget(field, value)
            form.addRow(_labelled(field), widget)
        layout.addLayout(form)
        layout.addStretch(1)

    def values(self) -> dict[str, Any]:
        """Return the edited options (without ``enable``), keyed as the config file stores them."""
        return {name: read() for name, read in self._readers.items()}

    def _widget(self, field: ConfigField, value: Any) -> QWidget:
        """Build the control for one scalar option and register how to read it back."""
        if field.kind == "bool":
            check = QCheckBox()
            check.setChecked(bool(value))
            check.setToolTip(field.help)
            self._readers[field.key] = check.isChecked
            return check
        if field.kind == "int":
            spin = QSpinBox()
            # A legit ``0`` bound is falsy, so test ``is None`` rather than ``or``.
            spin.setRange(
                0 if field.minimum is None else int(field.minimum),
                1_000_000 if field.maximum is None else int(field.maximum),
            )
            spin.setValue(int(value or 0))
            spin.setToolTip(field.help)
            self._readers[field.key] = spin.value
            return spin
        if field.kind == "float":
            double = QDoubleSpinBox()
            double.setDecimals(2)
            double.setSingleStep(0.05)
            double.setRange(
                0.0 if field.minimum is None else float(field.minimum),
                1_000_000.0 if field.maximum is None else float(field.maximum),
            )
            double.setValue(float(value or 0.0))
            double.setToolTip(field.help)
            self._readers[field.key] = double.value
            return double
        # Everything else is text — no bundled task option needs a picker, and a stringy value
        # round-trips unharmed through a line edit.
        line = QLineEdit(str(value or ""))
        line.setToolTip(field.help)
        self._readers[field.key] = line.text
        return line


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

        self._tasks = self._configured_tasks()
        self._editors: dict[str, _TaskOptionsEditor] = {}

        root = QVBoxLayout(self)
        root.addWidget(
            _hint(
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
            _hint(
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

        A saved option the model rejects must not lock the user out of the very dialog that
        would fix it, so an invalid section falls back to the shipped defaults (and is logged).
        """
        settings = self._repo.feature_settings(_PLUGIN_ID) or NoteMaintenanceSettings()
        try:
            return build_tasks(settings.tasks)
        except ValidationError:
            logger.exception(
                "note_maintenance: invalid task config; showing the defaults"
            )
            return build_tasks({})

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
        layout.addWidget(_hint(self, "Unticked tasks are skipped by every run."))
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
        """Write every task's switch, order and options back into the plugin's config section."""
        tasks: dict[str, dict[str, Any]] = {}
        for row, task in enumerate(self._tasks):
            item = self._task_list.item(row)
            tasks[task.task_id] = {
                "enable": item.checkState() == Qt.CheckState.Checked,
                **self._editors[task.task_id].values(),
            }
        try:
            self._repo.update_section(_PLUGIN_ID, {"tasks": tasks})
        except OSError:
            logger.exception("note_maintenance: could not save settings")
        self.accept()


def _hint(widget: QWidget, text: str) -> QLabel:
    """A secondary-text label that stays readable in BOTH themes.

    ``palette(mid)`` (the obvious choice) resolves to a near-black under Anki's dark theme,
    which is invisible on its dark background. Deriving from the window's ACTUAL text colour
    and softening it with alpha keeps the contrast direction correct whatever the theme.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    color = widget.palette().color(QPalette.ColorRole.WindowText)
    label.setStyleSheet(
        f"color: rgba({color.red()}, {color.green()}, {color.blue()}, 165);"
    )
    return label


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
