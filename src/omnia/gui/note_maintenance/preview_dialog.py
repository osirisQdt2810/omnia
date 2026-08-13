"""The maintenance preview: review every pending change, untick what you don't want, apply.

This dialog is the safety gate of the whole feature. A maintenance run can rewrite thousands
of fields, so the runner only ever produces a *proposal* and nothing reaches the collection
until it has been shown here, field by field, and confirmed.

Shape: a two-level tree — one row per note, carrying a tick that governs the whole note, and
under it one row per changed field with its own tick and the old and the new text with the
differing words marked (struck-through red for what goes, green for what arrives). Apply writes
ONLY the ticked changes, through the plugin's ``CollectionOp`` applier, so the whole batch is a
single undo step.

An ITEM view rather than a widget per note, because the promise above has to hold for the batch
this feature is built for: a few widgets per field stop scaling long before 5 000 notes, and a
dialog that renders a slice while applying everything would be showing the user 4% of what it
writes. Items are cheap and only the visible rows are painted, so the WHOLE plan is listed.

Thin Qt glue by design: the inclusion state and the filtered plan live in the pure
:class:`~omnia.plugins.note_maintenance.preview.PreviewModel`, which unit-tests headless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from aqt.qt import (  # type: ignore[attr-defined]
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPalette,
    QSize,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    Qt,
    QTextDocument,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from aqt.theme import theme_manager

from omnia.plugins.note_maintenance.apply import ChangeApplier
from omnia.plugins.note_maintenance.preview import NotePreview, PreviewModel
from omnia.plugins.note_maintenance.runner import ChangePlan

# The column holding the marked-up before/after text (column 0 holds the ticks and labels).
_DIFF_COLUMN = 1
# Enough for "Note 1750124928394 — 3 field(s)" and the longest usual field name.
_LABEL_COLUMN_WIDTH = 260


@dataclass(frozen=True)
class _DiffPalette:
    """The inline styles the diff marks are drawn with, chosen for the active theme.

    ``diff.py`` marks removed/added runs as ``<del>``/``<ins>`` — semantic tags Qt's rich text
    engine does not style — so the colours are applied here, where the theme is known. Anki's
    dark theme needs the opposite lightness, hence :meth:`current`.
    """

    removed: str
    added: str

    @classmethod
    def current(cls) -> _DiffPalette:
        """Return the palette matching Anki's active (day/night) theme."""
        if theme_manager.night_mode:
            return cls(
                removed="background-color:#5a2027; color:#ffb4ab; text-decoration:line-through;",
                added="background-color:#1e4620; color:#a5d6a7;",
            )
        return cls(
            removed="background-color:#ffd7d5; color:#82071e; text-decoration:line-through;",
            added="background-color:#d3f2d8; color:#0a5c1f;",
        )

    def rich(self, diff_html: str) -> str:
        """Return ``diff_html`` with its ``<del>``/``<ins>`` marks styled for this theme."""
        for tag, style in (("del", self.removed), ("ins", self.added)):
            diff_html = diff_html.replace(
                f"<{tag}>", f'<span style="{style}">'
            ).replace(f"</{tag}>", "</span>")
        return diff_html

    def cell(self, before_html: str, after_html: str) -> str:
        """Return one field's diff cell: the old line, then the new one, marks styled."""
        return (
            f"<div>− {self.rich(before_html)}</div>"
            f"<div>+ {self.rich(after_html)}</div>"
        )


class _DiffDelegate(QStyledItemDelegate):
    """Paints the diff column as rich text (an item view draws plain text otherwise).

    One :class:`QTextDocument` per painted cell is what turns the marks into coloured,
    struck-through text — and it is built only for the rows actually on screen, which is what
    lets the dialog list the whole plan instead of a slice of it. Constructed with the view as
    its parent: that is where the wrap width comes from when Qt asks for a size before layout.
    """

    def paint(self, painter: Any, option: Any, index: Any) -> None:
        """Draw the row's background through the style, then the cell's HTML over it."""
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        markup = styled.text
        styled.text = ""  # the document draws the text; the style must not draw it too
        widget = styled.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, styled, painter, widget
        )
        painter.save()
        painter.translate(styled.rect.topLeft())
        # Text the diff did NOT mark takes the painter's pen colour, so it has to come from the
        # item's palette — the default (black) is invisible on Anki's dark theme.
        painter.setPen(styled.palette.color(QPalette.ColorRole.Text))
        self._document(markup, styled.rect.width()).drawContents(painter)
        painter.restore()

    def sizeHint(self, option: Any, index: Any) -> QSize:  # noqa: N802
        """Return the space the marked-up text needs at the column's current width.

        (``sizeHint`` mirrors Qt's method name, hence the naming exemption.)
        """
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        document = self._document(styled.text, self._wrap_width(styled))
        return QSize(int(document.idealWidth()), int(document.size().height()))

    def _wrap_width(self, styled: Any) -> int:
        """The width to wrap at: the cell's own, or the column's when Qt hasn't sized it yet."""
        if styled.rect.width() > 0:
            return int(styled.rect.width())
        view = self.parent()
        return int(view.columnWidth(_DIFF_COLUMN)) if view is not None else 0

    @staticmethod
    def _document(markup: str, width: int) -> QTextDocument:
        """Return the rendered document for one cell (wrapped to ``width`` when known)."""
        document = QTextDocument()
        document.setDocumentMargin(2)
        document.setHtml(markup)
        if width > 0:
            document.setTextWidth(width)
        return document


class _PreviewTree(QTreeWidget):
    """The whole plan as a two-level item view, kept in step with the pure preview model.

    The tree owns no decision: a tick is forwarded to the :class:`NotePreview` that owns it, and
    the note-level tri-state is read back from that model, so what Apply writes and what the
    user sees can never drift apart.
    """

    def __init__(
        self,
        model: PreviewModel,
        on_changed: Callable[[], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the tree over ``model``.

        Args:
            model: The reviewable plan (every note of it is listed).
            on_changed: Called after every tick so the dialog can refresh its summary.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._model = model
        self._on_changed = on_changed

        self.setColumnCount(2)
        self.setHeaderLabels(["Note / field", "− before   + after"])
        self.setItemDelegateForColumn(_DIFF_COLUMN, _DiffDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        header = self.header()
        # Interactive, NOT ResizeToContents: sizing a column to its contents measures every
        # row, which is precisely what a 5 000-note plan must not pay for on every layout.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_DIFF_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, _LABEL_COLUMN_WIDTH)
        # Row heights depend on the diff column's width, so re-lay them out when it changes.
        header.sectionResized.connect(self._on_section_resized)

        self._populate()
        self.expandAll()
        # Connected AFTER the fill: setting the initial ticks would fire it once per row.
        self.itemChanged.connect(self._on_item_changed)

    def _populate(self) -> None:
        """Add one row per note and one child row per changed field — the whole plan."""
        palette = _DiffPalette.current()
        for position, preview in enumerate(self._model.notes):
            note_item = QTreeWidgetItem(
                self, [f"Note {preview.note_id} — {len(preview.rows)} field(s)"]
            )
            note_item.setData(0, Qt.ItemDataRole.UserRole, position)
            self._make_checkable(note_item)
            for row in preview.rows:
                field_item = QTreeWidgetItem(
                    note_item,
                    [row.field, palette.cell(row.before_html, row.after_html)],
                )
                field_item.setData(0, Qt.ItemDataRole.UserRole, (position, row.field))
                self._make_checkable(field_item)

    @staticmethod
    def _make_checkable(item: QTreeWidgetItem) -> None:
        """Give ``item`` a tick, ticked (what the user reviewed is what Apply writes)."""
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Route a tick to the model: note rows govern their fields, field rows their own."""
        if column != 0:
            return
        included = item.checkState(0) == Qt.CheckState.Checked
        if item.parent() is None:
            self._set_note(item, included)
        else:
            self._set_field(item, included)
        self._on_changed()

    def _set_note(self, item: QTreeWidgetItem, included: bool) -> None:
        """The note's tick sets every field of the note (never leaves it partly ticked)."""
        preview = self._preview(item)
        preview.include_all(included)
        state = Qt.CheckState.Checked if included else Qt.CheckState.Unchecked
        # The model already knows; muting the tree keeps this from re-entering per field.
        self.blockSignals(True)
        for row in range(item.childCount()):
            item.child(row).setCheckState(0, state)
        self.blockSignals(False)

    def _set_field(self, item: QTreeWidgetItem, included: bool) -> None:
        """Include or exclude one field, and re-state its note's tick."""
        position, field = item.data(0, Qt.ItemDataRole.UserRole)
        preview = self._model.notes[int(position)]
        preview.set_included(str(field), included)
        self.blockSignals(True)
        item.parent().setCheckState(0, self._note_state(preview))
        self.blockSignals(False)

    def _preview(self, item: QTreeWidgetItem) -> NotePreview:
        """The :class:`NotePreview` a note row stands for."""
        return self._model.notes[int(item.data(0, Qt.ItemDataRole.UserRole))]

    @staticmethod
    def _note_state(preview: NotePreview) -> Qt.CheckState:
        """Reflect the field ticks in the note's tick (all / none / partial)."""
        if preview.is_partly_included:
            return Qt.CheckState.PartiallyChecked
        if preview.is_fully_included:
            return Qt.CheckState.Checked
        return Qt.CheckState.Unchecked

    def _on_section_resized(self, column: int, _old: int, _new: int) -> None:
        """Re-lay the rows out when the diff column changes width (their height depends on it)."""
        if column == _DIFF_COLUMN:
            self.doItemsLayout()


class MaintenancePreviewDialog(QDialog):
    """Shows every pending change grouped by note, and applies only what stays ticked."""

    def __init__(self, plan: ChangePlan, parent: Optional[QWidget] = None) -> None:
        """Build the preview from a runner plan.

        Args:
            plan: What the maintenance run WOULD change (nothing is written yet).
            parent: Parent widget (the Browser).
        """
        super().__init__(parent)
        self._model = PreviewModel(plan)
        self.setWindowTitle("Omnia — maintain notes")
        self.resize(880, 620)

        root = QVBoxLayout(self)
        root.addWidget(
            self._hint(
                "Nothing has been written yet. Untick anything you want to keep as it is, "
                "then Apply — the whole batch lands as ONE undo step (Ctrl+Z puts it back)."
            )
        )
        self._summary = QLabel()
        root.addWidget(self._summary)
        root.addWidget(_PreviewTree(self._model, self._refresh_summary, self), 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        # Apply carries ApplyRole, which does NOT fire ``accepted`` — connect the button itself.
        self._apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self._apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_summary()

    def _hint(self, text: str) -> QLabel:
        """A secondary-text label that stays readable in BOTH themes.

        Derived from the window's ACTUAL text colour and softened with alpha, so the contrast
        direction is right whatever the theme (``palette(mid)`` goes near-black on dark).
        """
        label = QLabel(text)
        label.setWordWrap(True)
        color = self.palette().color(QPalette.ColorRole.WindowText)
        label.setStyleSheet(
            f"color: rgba({color.red()}, {color.green()}, {color.blue()}, 165);"
        )
        return label

    def _refresh_summary(self) -> None:
        """Restate what Apply would do, and disable it when nothing is ticked."""
        notes = self._model.selected_note_count
        fields = self._model.selected_field_count
        self._summary.setText(
            f"<b>{notes}</b> note(s), <b>{fields}</b> field(s) selected"
        )
        self._apply_button.setEnabled(bool(fields))

    def _apply(self) -> None:
        """Write the ticked changes as one undoable batch, then close."""
        plan = self._model.selected_plan()
        if plan.is_empty:
            return
        # Parented to the Browser, not to this dialog: the op outlives the close below.
        ChangeApplier(plan).run(
            parent=self.parentWidget() or self, on_done=_report_applied
        )
        self.accept()


def _report_applied(count: int) -> None:
    """Tell the user how many notes were written (module-level: the dialog is gone by then)."""
    from aqt.utils import tooltip

    tooltip(f"Omnia: {count} note(s) updated — Ctrl+Z undoes the batch.")
