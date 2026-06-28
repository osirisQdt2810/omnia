"""The smart_notes settings dialog — a list of generation rules + advanced toggles.

Each rule is added/edited via the rich per-rule :class:`PromptDialog`; this outer dialog lists
the configured rules with Add/Edit/Remove and the 💬/🔈/🖼️ new-rule affordances (text/tts/
image), plus the advanced toggles (Allow-empty-fields, Regenerate-when-batching, Overwrite,
Generate-at-review). It reads/writes the rules through the :class:`ConfigRepository`, building
a validated :class:`SmartNotesSettings` on OK. Pure Qt glue — only loaded inside Anki.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.qt import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.core.config.models import SmartNotesFieldRule

_KIND_ICON = {"text": "💬", "image": "🖼️", "tts": "🔈"}


class SmartNotesDialog(QDialog):
    """Edit the smart_notes field-generation rules as a list, persisting via the repo."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        super().__init__(parent)
        self._repo = repo
        settings = repo.feature_settings("smart_notes")
        # A working copy of the rules; mutated by the per-rule dialog, persisted on OK.
        self._rules: list[SmartNotesFieldRule] = (
            [rule.model_copy() for rule in settings.fields] if settings else []
        )
        self.setWindowTitle("Smart Notes ✨")
        self.setMinimumWidth(640)
        self.setMinimumHeight(480)
        self._build(settings)
        self._refresh_list()

    def _build(self, settings: Any) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(8)

        outer.addWidget(
            QLabel("<h3>✨ Smart Fields — generate text, audio, and images</h3>")
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _i: self._edit_selected())
        outer.addWidget(self._list, 1)

        row = QHBoxLayout()
        edit = QPushButton("Edit")
        edit.clicked.connect(self._edit_selected)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        row.addWidget(edit)
        row.addWidget(remove)
        row.addStretch(1)
        for kind in ("text", "tts", "image"):
            label = {"text": "New Text", "tts": "New TTS", "image": "New Image"}[kind]
            add = QPushButton(f"{_KIND_ICON[kind]} {label}")
            add.clicked.connect(lambda _c=False, k=kind: self._add(k))
            row.addWidget(add)
        outer.addLayout(row)

        self._allow_empty = QCheckBox("Allow empty source fields (generate anyway)")
        self._regen = QCheckBox("Regenerate already-filled fields when batching")
        self._overwrite = QCheckBox("Overwrite existing target fields")
        self._at_review = QCheckBox("Generate empty smart fields at review time")
        if settings is not None:
            self._allow_empty.setChecked(settings.allow_empty_fields)
            self._regen.setChecked(settings.regenerate_when_batching)
            self._overwrite.setChecked(settings.overwrite)
            self._at_review.setChecked(settings.generate_at_review)
        for box in (self._allow_empty, self._regen, self._overwrite, self._at_review):
            outer.addWidget(box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # --- rule list -------------------------------------------------------------------
    def _refresh_list(self) -> None:
        self._list.clear()
        for rule in self._rules:
            self._list.addItem(QListWidgetItem(_describe_rule(rule)))

    def _selected_index(self) -> int:
        return self._list.currentRow()

    def _add(self, kind: str) -> None:
        from omnia.core.config.models import SmartNotesFieldRule

        rule = SmartNotesFieldRule(kind=kind)
        self._open_prompt_dialog(rule, lambda saved: self._append(saved))

    def _edit_selected(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        self._open_prompt_dialog(
            self._rules[index].model_copy(),
            lambda saved, i=index: self._replace(i, saved),
        )

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        del self._rules[index]
        self._refresh_list()

    def _append(self, rule: SmartNotesFieldRule) -> None:
        self._rules.append(rule)
        self._refresh_list()

    def _replace(self, index: int, rule: SmartNotesFieldRule) -> None:
        self._rules[index] = rule
        self._refresh_list()

    def _open_prompt_dialog(self, rule: Any, on_save: Any) -> None:
        from omnia.gui.smart_notes_prompt_dialog import PromptDialog

        PromptDialog(self._repo, rule, on_save, self).exec()

    # --- persist ---------------------------------------------------------------------
    def _on_accept(self) -> None:
        from aqt.utils import showWarning
        from pydantic import ValidationError

        from omnia.core.config.models import SmartNotesSettings

        try:
            settings = SmartNotesSettings(
                fields=[rule.model_dump() for rule in self._rules],
                allow_empty_fields=self._allow_empty.isChecked(),
                regenerate_when_batching=self._regen.isChecked(),
                overwrite=self._overwrite.isChecked(),
                generate_at_review=self._at_review.isChecked(),
            )
        except ValidationError as exc:
            showWarning(f"Omnia: invalid field rules — fix and try again.\n\n{exc}")
            return  # keep the dialog open so the user can correct the rules
        self._repo.update_section("smart_notes", settings.model_dump())
        self.accept()


def _describe_rule(rule: SmartNotesFieldRule) -> str:
    """A one-line summary of a rule for the list (icon, type → field, enabled state)."""
    icon = _KIND_ICON.get(rule.kind, "✨")
    note_type = rule.note_type or "Any note type"
    state = "" if rule.enabled else "  (disabled)"
    deck = "" if rule.deck_id is None else "  [deck-scoped]"
    return f"{icon}  {note_type} → {rule.target_field or '?'}{deck}{state}"
