"""Field-aware controls for the Note Maintenance panel: pick a field, don't type one.

The shared :class:`~omnia.gui.config_form.ConfigFieldEditor` renders an option by its KIND
(number, text, switch) and cannot know about note types — so an option holding a field name
came out as a text box, where a typo failed silently at run time and nothing told the user
which fields exist. These two controls take the selected note type's real field names and
offer them.

Thin glue by design: which entries a dropdown has — and what happens to a stored value the
note type no longer has — is decided by the pure
:class:`~omnia.plugins.note_maintenance.field_choices.FieldChoices`, so that rule is testable
headless and cannot drift between the single-field control and the map's two columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Optional

from aqt.qt import (  # type: ignore[attr-defined]
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from omnia.plugins.note_maintenance.field_choices import FieldChoices

#: What an empty target means in a ``{source: target}`` map — the option's own blank.
_IN_PLACE_LABEL = "(rewrite the source field)"


class NoteFieldCombo(QComboBox):
    """A dropdown of ONE note type's fields, holding the stored value even if it is stale."""

    def __init__(
        self,
        choices: FieldChoices,
        value: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the dropdown.

        Args:
            choices: The entries to offer (the note type's fields, plus the blank/stale rules).
            value: The option's stored value — always selectable, whether or not the note type
                still has it.
            parent: Parent widget.
        """
        super().__init__(parent)
        current = 0
        for index, choice in enumerate(choices.entries(str(value or ""))):
            # The VALUE rides on the item as data: the label of a stale entry is decorated, and
            # reading the label back would write the decoration into the user's config.
            self.addItem(choice.label, choice.value)
            if choice.value == value:
                current = index
        self.setCurrentIndex(current)

    def value(self) -> str:
        """Return the selected field name (``""`` for the blank entry)."""
        return str(self.currentData() or "")


class FieldMapEditor(QWidget):
    """Edits a ``{source field: target field}`` option as two columns of dropdowns.

    Each row says "read THIS field, write the result to THAT one", and an empty target means
    "rewrite the source in place" — so the target column offers a blank entry and the source
    column does not.
    """

    def __init__(
        self,
        fields: Sequence[str],
        values: Mapping[str, str],
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the table.

        Args:
            fields: The selected note type's field names.
            values: The current ``{source: target}`` mapping.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._sources = FieldChoices(fields)
        self._targets = FieldChoices(fields, blank_label=_IN_PLACE_LABEL)

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
            self._add_row(str(source), str(target or ""))

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
            source = self._combo(row, 0).value()
            if source:
                mapping[source] = self._combo(row, 1).value()
        return mapping

    def _add_row(self, source: str, target: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setCellWidget(row, 0, NoteFieldCombo(self._sources, source))
        self._table.setCellWidget(row, 1, NoteFieldCombo(self._targets, target))

    def _remove_selected(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)

    def _combo(self, row: int, column: int) -> NoteFieldCombo:
        return self._table.cellWidget(row, column)
