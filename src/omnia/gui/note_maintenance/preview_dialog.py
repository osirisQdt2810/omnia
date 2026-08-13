"""The maintenance preview: review every pending change, untick what you don't want, apply.

This dialog is the safety gate of the whole feature. A maintenance run can rewrite thousands
of fields, so the runner only ever produces a *proposal* and nothing reaches the collection
until it has been shown here, field by field, and confirmed.

Shape: one block per note, each with a tick that governs the whole note and one tick per field,
and under each field the old and the new text with the differing words marked (struck-through
red for what goes, green for what arrives). Apply writes ONLY the ticked changes, through the
plugin's ``CollectionOp`` applier, so the whole batch is a single undo step.

Thin Qt glue by design: the inclusion state and the filtered plan live in the pure
:class:`~omnia.plugins.note_maintenance.preview.PreviewModel`, which unit-tests headless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from aqt.qt import (  # type: ignore[attr-defined]
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPalette,
    QScrollArea,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.theme import theme_manager

from omnia.plugins.note_maintenance.apply import ChangeApplier
from omnia.plugins.note_maintenance.preview import NotePreview, PreviewModel
from omnia.plugins.note_maintenance.runner import ChangePlan

# A widget per note stops scaling long before the 5 000-note batch this feature is built for,
# so only the first slice is rendered. The rest stay in the plan (and in the counts) — they are
# simply not individually reviewable in one sitting.
_MAX_RENDERED_NOTES = 200


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


class _NoteGroup(QFrame):
    """One note's block: a tick that governs the note, and one ticked diff row per field."""

    def __init__(
        self,
        preview: NotePreview,
        palette: _DiffPalette,
        on_changed: Callable[[], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the block.

        Args:
            preview: The note's rows + inclusion state (this widget's model).
            palette: The theme-appropriate diff colours.
            on_changed: Called after every tick so the dialog can refresh its summary.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._preview = preview
        self._palette = palette
        self._on_changed = on_changed
        self._boxes: dict[str, QCheckBox] = {}
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        self._header = QCheckBox(
            f"Note {preview.note_id} — {len(preview.rows)} field(s)"
        )
        self._header.setTristate(True)  # so a partly-ticked note reads as partly ticked
        self._header.setChecked(True)
        self._header.clicked.connect(self._on_header_clicked)
        layout.addWidget(self._header)
        for row in preview.rows:
            layout.addWidget(
                self._field_row(row.field, row.before_html, row.after_html)
            )

    def _field_row(self, field: str, before_html: str, after_html: str) -> QWidget:
        """One field: its tick, then the old and the new text with the changes marked."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(20, 0, 0, 6)  # indented under the note's tick

        box = QCheckBox(field)
        box.setChecked(True)
        box.toggled.connect(
            lambda checked, name=field: self._on_field_toggled(name, checked)
        )
        self._boxes[field] = box
        layout.addWidget(box)
        layout.addWidget(self._diff_line("−", before_html))
        layout.addWidget(self._diff_line("+", after_html))
        return holder

    def _diff_line(self, marker: str, diff_html: str) -> QWidget:
        """A single before/after line: its ``−``/``+`` marker and the marked-up text."""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(20, 0, 0, 0)
        gutter = QLabel(marker)
        gutter.setFixedWidth(14)
        gutter.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(gutter)
        text = QLabel(self._palette.rich(diff_html))
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text, 1)
        return holder

    def _on_header_clicked(self, checked: bool) -> None:
        """The note's tick sets every field of the note (never leaves it partly ticked)."""
        self._preview.include_all(checked)
        for box in self._boxes.values():
            # The model already knows; muting the box keeps this from re-entering per field.
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)
        self._sync_header()
        self._on_changed()

    def _on_field_toggled(self, field: str, checked: bool) -> None:
        """Include or exclude one field, and re-state the note's tick."""
        self._preview.set_included(field, checked)
        self._sync_header()
        self._on_changed()

    def _sync_header(self) -> None:
        """Reflect the field ticks in the note's tick (all / none / partial)."""
        if self._preview.is_partly_included:
            state = Qt.CheckState.PartiallyChecked
        elif self._preview.is_fully_included:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.Unchecked
        self._header.blockSignals(True)
        self._header.setCheckState(state)
        self._header.blockSignals(False)


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
        root.addWidget(self._groups(), 1)

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

    def _groups(self) -> QWidget:
        """The scrollable list of per-note blocks (capped — see :data:`_MAX_RENDERED_NOTES`)."""
        palette = _DiffPalette.current()
        container = QWidget()
        layout = QVBoxLayout(container)
        shown = self._model.notes[:_MAX_RENDERED_NOTES]
        for preview in shown:
            layout.addWidget(_NoteGroup(preview, palette, self._refresh_summary))
        hidden = len(self._model.notes) - len(shown)
        if hidden:
            layout.addWidget(
                self._hint(
                    f"… and {hidden} more changed note(s) not shown. They ARE included in "
                    "Apply — narrow the selection if you want to review them one by one."
                )
            )
        layout.addStretch(1)

        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setWidget(container)
        return scroller

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
