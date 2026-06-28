"""The smart_notes field-mapping dialog — a table of generation rules.

Each row maps (Note Type, Source Field, Target Field, Kind, Prompt) to one generation rule.
The dialog reads the current rules from the :class:`ConfigRepository`, lets the user edit/add/
remove rows, validates them by constructing :class:`SmartNotesSettings`, and persists on OK.
Pure Qt glue — only loaded inside Anki; row↔rule normalisation lives in ``logic.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from omnia.features.smart_notes.logic import rows_to_rules, rules_to_rows

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository

_KINDS = ("text", "image", "tts")
_COLUMNS = ("Note Type", "Source Field", "Target Field", "Kind", "Prompt")
# Column index of the per-row Kind combo box; the others are plain text cells.
_KIND_COL = 3
# Maps each table column to its rule-dict key (Kind is handled separately via the combo).
_TEXT_KEYS = {0: "note_type", 1: "source_field", 2: "target_field", 4: "prompt"}


class SmartNotesDialog(QDialog):
    """Edit the smart_notes field-generation rules as a table, persisting via the repo."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Smart Notes — field mapping")
        self.setMinimumWidth(720)
        self.setMinimumHeight(420)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(8)

        intro = QLabel(
            "Map a source field to a target field per note type. Kind picks how the "
            "target is filled; Prompt may reference fields as {{FieldName}}."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        outer.addWidget(self._table, 1)

        settings = self._repo.feature_settings("smart_notes")
        for row in rules_to_rows(list(settings.fields) if settings else []):
            self._append_row(row)

        row_buttons = QHBoxLayout()
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self._append_row({}))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        outer.addLayout(row_buttons)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _append_row(self, values: dict[str, str]) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, key in _TEXT_KEYS.items():
            self._table.setItem(row, col, QTableWidgetItem(str(values.get(key, ""))))
        combo = QComboBox()
        combo.addItems(list(_KINDS))
        kind = values.get("kind", "text")
        combo.setCurrentText(kind if kind in _KINDS else "text")
        self._table.setCellWidget(row, _KIND_COL, combo)

    def _remove_selected(self) -> None:
        # Delete from the bottom up so earlier row indices stay valid.
        rows = sorted(
            {index.row() for index in self._table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self._table.removeRow(row)

    def _collect_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for row in range(self._table.rowCount()):
            entry: dict[str, str] = {}
            for col, key in _TEXT_KEYS.items():
                item = self._table.item(row, col)
                entry[key] = item.text() if item is not None else ""
            combo = self._table.cellWidget(row, _KIND_COL)
            entry["kind"] = combo.currentText() if combo is not None else "text"
            rows.append(entry)
        return rows

    def _on_accept(self) -> None:
        from aqt.utils import showWarning
        from pydantic import ValidationError

        from omnia.core.config.models import SmartNotesSettings

        rows = rows_to_rules(self._collect_rows())
        try:
            SmartNotesSettings(fields=rows)
        except ValidationError as exc:
            showWarning(f"Omnia: invalid field rules — fix and try again.\n\n{exc}")
            return  # keep the dialog open so the user can correct the rows
        self._repo.update_section("smart_notes", {"fields": rows})
        self.accept()
