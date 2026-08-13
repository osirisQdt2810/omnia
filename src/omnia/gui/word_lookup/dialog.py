"""Bespoke settings dialog for Word Lookup: pick note types, then their fields.

The generic settings form can render flat values, but this plugin's useful settings are
**per note type** — which fields to search in, and which to show — which is a mapping the flat
form cannot express. So the plugin declares this dialog instead.

Shape: note types down the left (ticked = searchable), and for whichever one is highlighted, two
field lists on the right — *Search in* and *Show* — each with an **Auto** button. Auto is what
makes a 35-field note type usable: it ticks the sensible default (the headword field for search;
the first ``max_fields`` for display) so nobody has to reason about 35 checkboxes.

Leaving a list empty is meaningful and is spelled out in the UI: empty *Search in* searches every
field of that note type, empty *Show* falls back to the automatic pick.
"""

from __future__ import annotations

from typing import Any, Optional

from aqt.qt import (  # type: ignore[attr-defined]
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    Qt,
    QVBoxLayout,
    QWidget,
)

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.widgets import hint_label
from omnia.plugins.word_lookup.config import WordLookupSettings

logger = get_logger("word_lookup")

_PLUGIN_ID = "word_lookup"
# Field-name hints used ONLY to pre-tick Auto for the search list. Not a hard rule: the user can
# tick anything, and a note type matching none falls back to the note type's first field.
_HEADWORD_HINTS = ("word", "front", "term", "expression", "vocab", "headword", "kanji")


class WordLookupSettingsDialog(QDialog):
    """Pick searchable note types and, per note type, the fields to search and to show."""

    def __init__(self, repo: Any, parent: Optional[QWidget] = None) -> None:
        """Build the dialog from the plugin's saved settings.

        Args:
            repo: The ``ConfigRepository`` (read on open, written on accept).
            parent: Parent widget.
        """
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Word Lookup — settings")
        self.resize(760, 520)

        settings = repo.feature_settings(_PLUGIN_ID) or WordLookupSettings()
        self._search_fields: dict[str, list[str]] = {
            k: list(v) for k, v in dict(settings.search_fields).items()
        }
        self._display_fields: dict[str, list[str]] = {
            k: list(v) for k, v in dict(settings.display_fields).items()
        }
        self._current: str = ""

        root = QVBoxLayout(self)
        root.addWidget(
            hint_label(
                self,
                "Tick the note types the desktop clipper's magnifier should search. "
                "Select one to choose its fields.",
            )
        )

        columns = QHBoxLayout()
        columns.addWidget(self._note_type_column(settings), 1)
        columns.addWidget(
            self._field_column(
                "Search in",
                "The word must appear as a WHOLE WORD: 'port' finds 'port of call', "
                "not 'important'.\n"
                "Empty = search every field, substring allowed.\n"
                "Case never matters (PORT = Port = port).",
                on_auto=self._auto_search,
                attr="_search_list",
            ),
            1,
        )
        columns.addWidget(
            self._field_column(
                "Preview",
                "Shown in this order in the lookup panel. "
                "Empty = automatic (the first N non-empty fields).",
                on_auto=self._auto_display,
                attr="_display_list",
            ),
            1,
        )
        root.addLayout(columns, 1)

        self._word_forms = QCheckBox(
            "Also match other forms (loved -> love, studies -> study)"
        )
        self._word_forms.setChecked(bool(settings.match_word_forms))
        self._word_forms.setToolTip(
            "Double-clicking an inflected word still finds the card filed under its base form."
        )
        root.addWidget(self._word_forms)

        self._max_results = self._spin(1, 25, int(settings.max_results))
        self._max_fields = self._spin(1, 30, int(settings.max_fields))
        self._port = self._spin(1024, 65535, int(settings.port))
        numbers = QHBoxLayout()
        numbers.addWidget(QLabel("Max results"))
        numbers.addWidget(self._max_results)
        numbers.addSpacing(16)
        numbers.addWidget(QLabel("Auto-show fields"))
        numbers.addWidget(self._max_fields)
        numbers.addSpacing(16)
        numbers.addWidget(QLabel("Service port"))
        numbers.addWidget(self._port)
        numbers.addStretch(1)
        root.addLayout(numbers)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._note_types.count():
            self._note_types.setCurrentRow(0)

    # -- construction helpers -------------------------------------------------------------

    @staticmethod
    def _spin(low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        return spin

    def _note_type_column(self, settings: WordLookupSettings) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Note types</b>"))

        self._note_types = QListWidget()
        self._note_types.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        enabled = {name.strip().lower() for name in settings.note_types}
        for name in self._all_note_types():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if name.strip().lower() in enabled
                else Qt.CheckState.Unchecked
            )
            self._note_types.addItem(item)
        self._note_types.currentItemChanged.connect(self._on_note_type_changed)
        layout.addWidget(self._note_types, 1)
        layout.addWidget(hint_label(self, "None ticked = search the whole collection."))
        return holder

    def _field_column(self, title: str, hint: str, on_auto: Any, attr: str) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{title}</b>"))
        header.addStretch(1)
        auto = QPushButton("Auto")
        auto.setToolTip("Tick the sensible default for this note type.")
        auto.clicked.connect(on_auto)
        header.addWidget(auto)
        layout.addLayout(header)
        listing = QListWidget()
        layout.addWidget(listing, 1)
        layout.addWidget(hint_label(self, hint))
        setattr(
            self, attr, listing
        )  # explicit, so renaming a column can't rewire the lists
        return holder

    @staticmethod
    def _all_note_types() -> list[str]:
        """Every note type name in the collection (empty on any failure — never crash setup)."""
        try:
            return list(anki_compat.note_type_names())
        except Exception:
            logger.exception("word_lookup: could not read note type names")
            return []

    @staticmethod
    def _fields_of(note_type: str) -> list[str]:
        try:
            return list(anki_compat.note_type_field_names(note_type))
        except Exception:
            logger.exception("word_lookup: could not read fields of %r", note_type)
            return []

    # -- per-note-type field editing ------------------------------------------------------

    def _on_note_type_changed(
        self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]
    ) -> None:
        """Persist the outgoing note type's ticks, then load the incoming one's."""
        if previous is not None:
            self._remember(previous.text())
        self._current = current.text() if current is not None else ""
        self._load_fields(self._current)

    def _remember(self, note_type: str) -> None:
        """Copy the two field lists back into the in-memory maps for ``note_type``."""
        if not note_type:
            return
        self._search_fields[note_type] = self._checked(self._search_list)
        self._display_fields[note_type] = self._checked(self._display_list)

    def _load_fields(self, note_type: str) -> None:
        fields = self._fields_of(note_type) if note_type else []
        self._fill(self._search_list, fields, self._search_fields.get(note_type, []))
        self._fill(self._display_list, fields, self._display_fields.get(note_type, []))

    @staticmethod
    def _fill(listing: QListWidget, fields: list[str], checked: list[str]) -> None:
        """Show ``fields`` as checkboxes, ticking those in ``checked`` (case-insensitive)."""
        listing.clear()
        wanted = {name.strip().lower() for name in checked}
        for name in fields:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if name.strip().lower() in wanted
                else Qt.CheckState.Unchecked
            )
            listing.addItem(item)

    @staticmethod
    def _checked(listing: QListWidget) -> list[str]:
        """The ticked field names, in list order."""
        return [
            listing.item(row).text()
            for row in range(listing.count())
            if listing.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _auto_search(self) -> None:
        """Tick the field most likely to hold the headword (falls back to the first field)."""
        fields = self._fields_of(self._current)
        if not fields:
            return
        picked = [
            name
            for name in fields
            if any(hint in name.strip().lower() for hint in _HEADWORD_HINTS)
        ]
        self._fill(self._search_list, fields, picked or fields[:1])

    def _auto_display(self) -> None:
        """Tick the first ``Auto-show fields`` fields — the automatic pick, made explicit."""
        fields = self._fields_of(self._current)
        if fields:
            self._fill(self._display_list, fields, fields[: self._max_fields.value()])

    # -- persistence ----------------------------------------------------------------------

    def _save(self) -> None:
        """Write every setting back into the plugin's config section and close."""
        self._remember(self._current)  # the visible note type has unsaved ticks
        note_types = [
            self._note_types.item(row).text()
            for row in range(self._note_types.count())
            if self._note_types.item(row).checkState() == Qt.CheckState.Checked
        ]
        # Drop empty entries so the config stays a record of real choices only.
        search = {k: v for k, v in self._search_fields.items() if v}
        display = {k: v for k, v in self._display_fields.items() if v}
        try:
            self._repo.update_section(
                _PLUGIN_ID,
                {
                    "note_types": note_types,
                    "search_fields": search,
                    "display_fields": display,
                    "match_word_forms": self._word_forms.isChecked(),
                    "max_results": self._max_results.value(),
                    "max_fields": self._max_fields.value(),
                    "port": self._port.value(),
                },
            )
        except Exception:
            logger.exception("word_lookup: could not save settings")
        self.accept()
