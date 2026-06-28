"""The rich per-rule editor for one smart_notes field rule (ports the reference PromptDialog).

A dedicated dialog for adding/editing a single :class:`SmartNotesFieldRule`: note-type, deck
("All decks" → ``deck_id=None``), target field, source field (TTS/derived), kind, a large
prompt editor with live ``{{Field}}`` hints, an Enabled toggle, and per-field provider/model/
voice overrides (empty = inherit the central config). Two best-effort extras run a provider
off the Qt main thread: "Test With Random Note" previews the rule's output, and "Write My
Prompt For Me" turns a short description into a prompt template.

Pure Qt glue — only imported inside Anki. The dialog calls ``on_save(rule)`` with the built
rule and accepts; the outer :class:`SmartNotesDialog` owns persistence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omnia.core import anki_compat
from omnia.core.logging import get_logger

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.core.config.models import SmartNotesFieldRule

_KINDS = ("text", "image", "tts")
_ALL_DECKS_LABEL = "All decks"
_TITLES = {
    "text": "Text Field",
    "image": "Image Smart Field",
    "tts": "Text to Speech Field",
}


class PromptDialog(QDialog):
    """Add/edit one smart_notes field rule with note-type/deck/field pickers + overrides."""

    def __init__(
        self,
        repo: ConfigRepository,
        rule: SmartNotesFieldRule,
        on_save: Callable[[SmartNotesFieldRule], None],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._rule = rule
        self._on_save = on_save
        self._log = get_logger("smart_notes")
        self._decks = anki_compat.deck_names()
        self.setWindowTitle(f"Smart Notes — {_TITLES.get(rule.kind, 'Field')}")
        self.setMinimumWidth(640)
        self.setMinimumHeight(560)
        self._build()
        self._reload_fields()

    # --- construction ----------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "General")
        tabs.addTab(self._build_options_tab(), "Options")
        outer.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        targets = QGroupBox()
        form = QFormLayout(targets)

        self._note_type_combo = QComboBox()
        self._note_type_combo.addItems(anki_compat.note_type_names())
        if self._rule.note_type:
            self._note_type_combo.setCurrentText(self._rule.note_type)
        self._note_type_combo.currentTextChanged.connect(self._reload_fields)
        form.addRow("Note Type", self._note_type_combo)

        self._deck_combo = QComboBox()
        self._deck_combo.addItem(_ALL_DECKS_LABEL, None)
        for deck_id, name in self._decks:
            self._deck_combo.addItem(name, deck_id)
        self._select_deck(self._rule.deck_id)
        form.addRow("Deck", self._deck_combo)

        self._kind_combo = QComboBox()
        self._kind_combo.addItems(list(_KINDS))
        self._kind_combo.setCurrentText(self._rule.kind)
        self._kind_combo.currentTextChanged.connect(self._on_kind_changed)
        form.addRow("Kind", self._kind_combo)

        self._target_combo = QComboBox()
        form.addRow("Field to Generate", self._target_combo)

        self._source_combo = QComboBox()
        self._source_row_label = QLabel("Source Field")
        form.addRow(self._source_row_label, self._source_combo)

        layout.addWidget(targets)

        # "Write My Prompt For Me" — a short description → an LLM-written prompt template.
        layout.addWidget(self._build_write_prompt_box())

        layout.addWidget(QLabel("Prompt (reference fields as {{FieldName}})"))
        self._prompt_edit = QPlainTextEdit(self._rule.prompt)
        self._prompt_edit.setMinimumHeight(120)
        self._prompt_edit.textChanged.connect(self._render_valid_fields)
        layout.addWidget(self._prompt_edit, 1)

        self._valid_fields = QLabel("")
        self._valid_fields.setWordWrap(True)
        layout.addWidget(self._valid_fields)

        self._enabled_box = QCheckBox("Enabled")
        self._enabled_box.setChecked(self._rule.enabled)
        enabled_hint = QLabel(
            "(Disabled rules are skipped in batches but can still be generated by right-click.)"
        )
        enabled_hint.setWordWrap(True)
        layout.addWidget(self._enabled_box)
        layout.addWidget(enabled_hint)

        self._test_button = QPushButton("Test With Random Note")
        self._test_button.clicked.connect(self._on_test)
        layout.addWidget(self._test_button)

        return page

    def _build_write_prompt_box(self) -> QWidget:
        box = QGroupBox("Write My Prompt For Me")
        row = QHBoxLayout(box)
        self._ai_prompt_input = QLineEdit()
        self._ai_prompt_input.setPlaceholderText(
            "Make a simple example sentence for this vocab…"
        )
        self._write_prompt_button = QPushButton("Write Prompt")
        self._write_prompt_button.clicked.connect(self._on_write_prompt)
        row.addWidget(self._ai_prompt_input, 1)
        row.addWidget(self._write_prompt_button)
        return box

    def _build_options_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(
            QLabel(
                "Per-field provider overrides — leave blank to inherit the central config."
            )
        )
        self._provider_edit = QLineEdit(self._rule.provider)
        self._provider_edit.setPlaceholderText("(inherit central provider)")
        form.addRow("Provider", self._provider_edit)
        self._model_edit = QLineEdit(self._rule.model)
        self._model_edit.setPlaceholderText("(inherit central model)")
        form.addRow("Model", self._model_edit)
        self._voice_edit = QLineEdit(self._rule.voice)
        self._voice_edit.setPlaceholderText("(inherit central voice)")
        form.addRow("Voice (TTS)", self._voice_edit)
        return page

    # --- dynamic state ---------------------------------------------------------------
    def _on_kind_changed(self, _kind: str) -> None:
        is_tts = self._kind_combo.currentText() == "tts"
        self._source_combo.setVisible(is_tts)
        self._source_row_label.setVisible(is_tts)

    def _reload_fields(self, *_a: Any) -> None:
        """Refresh the target/source field pickers for the selected note type."""
        note_type = self._note_type_combo.currentText()
        fields = anki_compat.note_type_field_names(note_type) if note_type else []
        self._set_combo_items(self._target_combo, fields, self._rule.target_field)
        self._set_combo_items(self._source_combo, fields, self._rule.source_field)
        self._on_kind_changed("")
        self._render_valid_fields()

    @staticmethod
    def _set_combo_items(combo: QComboBox, items: list[str], current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        if current and current in items:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def _select_deck(self, deck_id: Optional[int]) -> None:
        index = self._deck_combo.findData(deck_id)
        self._deck_combo.setCurrentIndex(index if index >= 0 else 0)

    def _render_valid_fields(self) -> None:
        note_type = self._note_type_combo.currentText()
        fields = anki_compat.note_type_field_names(note_type) if note_type else []
        target = self._target_combo.currentText()
        usable = [f for f in fields if f != target]
        refs = ", ".join(f"{{{{{name}}}}}" for name in usable)
        self._valid_fields.setText(f"Valid fields: {refs}" if refs else "")

    # --- best-effort provider actions ------------------------------------------------
    def _on_write_prompt(self) -> None:
        from aqt.utils import showWarning

        description = self._ai_prompt_input.text().strip()
        if not description:
            return
        target = self._target_combo.currentText()
        note_type = self._note_type_combo.currentText()
        instruction = (
            "Write a concise Anki Smart Notes prompt template. The note type is "
            f"{note_type!r} and the target field is {target!r}. Reference other fields with "
            "{{FieldName}}. Output only the prompt template, no commentary. The user wants: "
            f"{description}"
        )
        hub = self._repo_hub()
        if hub is None:
            return
        self._write_prompt_button.setEnabled(False)

        def op() -> str:
            return hub.llm(model=self._model_edit.text().strip()).generate_text(
                instruction
            )

        def on_success(text: str) -> None:
            self._write_prompt_button.setEnabled(True)
            self._prompt_edit.setPlainText(text.strip())

        def on_failure(exc: Exception) -> None:
            self._write_prompt_button.setEnabled(True)
            showWarning(f"Omnia: could not write a prompt:\n{exc}")

        anki_compat.run_in_background(
            op, on_success=on_success, on_failure=on_failure, label="Omnia: writing…"
        )

    def _on_test(self) -> None:
        from aqt.utils import showInfo, showWarning

        rule = self._collect_rule()
        if not rule.target_field:
            showWarning("Omnia: pick a target field first.")
            return
        note = anki_compat.random_note_of_type(rule.note_type or None, rule.deck_id)
        if note is None:
            showWarning("Omnia: need at least one note of this note type to test.")
            return
        fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        hub = self._repo_hub()
        if hub is None:
            return
        from omnia.features.smart_notes.logic import GenerationService

        service = GenerationService(hub)
        self._test_button.setEnabled(False)

        def op() -> Any:
            return service.generate(rule, fields)

        def on_success(result: Any) -> None:
            self._test_button.setEnabled(True)
            if result.kind == "text":
                showInfo(f"Generated text:\n\n{result.text}")
            elif result.kind == "tts":
                anki_compat.play_audio(result.data or b"", result.ext)
                showInfo("Generated audio — playing the preview.")
            else:
                showInfo("Generated an image (saved nowhere — this was a test).")

        def on_failure(exc: Exception) -> None:
            self._test_button.setEnabled(True)
            showWarning(f"Omnia: test generation failed:\n{exc}")

        anki_compat.run_in_background(
            op, on_success=on_success, on_failure=on_failure, label="Omnia: testing…"
        )

    def _repo_hub(self) -> Any:
        """Build a ProviderHub from the central config (None + a warning if it can't).

        The providers default to the add-on's real HTTP client, so no client is injected here
        (the same way the PluginManager builds the hub it hands plugins).
        """
        from aqt.utils import showWarning

        from omnia.core.providers import ProviderHub

        try:
            return ProviderHub(self._repo.llm_settings(), self._repo.tts_settings())
        except Exception as exc:  # boundary: surface bad provider config to the user
            self._log.exception("smart_notes: could not build provider hub")
            showWarning(f"Omnia: provider config error:\n{exc}")
            return None

    # --- accept ----------------------------------------------------------------------
    def _collect_rule(self) -> SmartNotesFieldRule:
        from omnia.core.config.models import SmartNotesFieldRule

        return SmartNotesFieldRule(
            note_type=self._note_type_combo.currentText(),
            source_field=(
                self._source_combo.currentText()
                if self._kind_combo.currentText() == "tts"
                else ""
            ),
            target_field=self._target_combo.currentText(),
            kind=self._kind_combo.currentText(),
            prompt=self._prompt_edit.toPlainText().strip(),
            deck_id=self._deck_combo.currentData(),
            enabled=self._enabled_box.isChecked(),
            provider=self._provider_edit.text().strip(),
            model=self._model_edit.text().strip(),
            voice=self._voice_edit.text().strip(),
        )

    def _on_accept(self) -> None:
        from aqt.utils import showWarning

        rule = self._collect_rule()
        if not rule.target_field:
            showWarning("Omnia: pick a Field to Generate.")
            return
        if rule.kind != "tts" and not rule.prompt:
            showWarning("Omnia: a text/image rule needs a prompt.")
            return
        if rule.kind == "tts" and not rule.source_field:
            showWarning("Omnia: a TTS rule needs a source field.")
            return
        self._on_save(rule)
        self.accept()
