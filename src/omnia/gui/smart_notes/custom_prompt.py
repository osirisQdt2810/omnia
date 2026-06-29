"""One-off "custom prompt" palettes for a single editor field (no saved rule).

Ports the reference add-on's ``custom_prompt`` dialogs: a small palette where the user types a
prompt (or picks a TTS source field), clicks Generate to run the configured provider off the
Qt main thread, previews the result, then saves it into the current field — without persisting
a smart-field rule. One :class:`CustomPromptDialog` covers all three kinds; the ``kind``
chooses the underlying :class:`GenerationService` path. Pure Qt glue — only imported in Anki.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from aqt.qt import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from omnia.core import anki_compat
from omnia.core.logging import get_logger

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository

logger = get_logger("smart_notes")

_TITLES = {"text": "💬 Custom Text", "image": "🖼️ Custom Image", "tts": "🔈 Custom TTS"}


class CustomPromptDialog(QDialog):
    """Generate content for one field from a one-off prompt, then save it on accept."""

    def __init__(
        self,
        repo: ConfigRepository,
        *,
        kind: str,
        note_type: str,
        field_names: list[str],
        target_field: str,
        on_save: Callable[[str], None],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._kind = kind
        self._note_type = note_type
        self._field_names = field_names
        self._target_field = target_field
        self._on_save = on_save
        self._result: Optional[Any] = None
        self.setWindowTitle(_TITLES.get(kind, "Custom Prompt"))
        self.setMinimumWidth(560)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(8)

        if self._kind == "tts":
            outer.addWidget(QLabel("Source Field"))
            self._source_combo = QComboBox()
            self._source_combo.addItems(self._field_names)
            self._source_combo.currentTextChanged.connect(self._on_source_changed)
            outer.addWidget(self._source_combo)

        outer.addWidget(QLabel("Prompt (reference fields as {{FieldName}})"))
        self._prompt_edit = QPlainTextEdit()
        if self._kind == "tts" and self._field_names:
            self._prompt_edit.setPlainText(f"{{{{{self._field_names[0]}}}}}")
        outer.addWidget(self._prompt_edit, 1)

        usable = ", ".join(
            f"{{{{{name}}}}}"
            for name in self._field_names
            if name != self._target_field
        )
        outer.addWidget(QLabel(f"Valid fields: {usable}"))

        self._generate_button = QPushButton("Generate")
        self._generate_button.clicked.connect(self._on_generate)
        outer.addWidget(self._generate_button)

        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        outer.addWidget(self._preview)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_save_result)
        self._buttons.rejected.connect(self.reject)
        self._save_enabled(False)
        outer.addWidget(self._buttons)

    def _on_source_changed(self, source: str) -> None:
        self._prompt_edit.setPlainText(f"{{{{{source}}}}}")

    def _save_enabled(self, enabled: bool) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(enabled)

    def _on_generate(self) -> None:
        from aqt.utils import showWarning

        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine import GenerationService

        prompt = self._prompt_edit.toPlainText().strip()
        if not prompt:
            return
        source = self._source_combo.currentText() if self._kind == "tts" else ""
        rule = SmartNotesFieldRule(
            note_type=self._note_type,
            kind=self._kind,
            target_field=self._target_field,
            source_field=source,
            prompt="" if self._kind == "tts" else prompt,
        )
        note = anki_compat.random_note_of_type(self._note_type or None)
        fields = (
            {name: note[name] for name in note.keys()}  # noqa: SIM118
            if note is not None
            else {}
        )
        # For TTS the source field is the templated input; seed it from the typed prompt so a
        # one-off TTS can speak literal text, not just an existing field's value.
        if self._kind == "tts" and source:
            fields[source] = prompt

        hub = self._build_hub(showWarning)
        if hub is None:
            return
        service = GenerationService(hub)
        self._generate_button.setEnabled(False)

        def op() -> Any:
            return service.generate(rule, fields)

        anki_compat.run_in_background(
            op,
            on_success=self._on_generated,
            on_failure=lambda exc: self._on_generate_failed(exc),
            label="Omnia: generating…",
        )

    def _on_generated(self, result: Any) -> None:
        self._generate_button.setEnabled(True)
        self._result = result
        if result.kind == "text":
            self._preview.setText(result.text or "")
        elif result.kind == "tts":
            anki_compat.play_audio(result.data or b"", result.ext)
            self._preview.setText("Generated audio — playing the preview.")
        else:
            self._preview.setText("Generated an image — Save to insert it.")
        self._save_enabled(True)

    def _on_generate_failed(self, exc: Exception) -> None:
        from aqt.utils import showWarning

        self._generate_button.setEnabled(True)
        showWarning(f"Omnia: generation failed:\n{exc}")

    def _on_save_result(self) -> None:
        if self._result is None:
            self.reject()
            return
        from omnia.plugins.smart_notes.integration.batch import materialize

        # nid 0 is fine: media filenames are namespaced by field, and a one-off has no note id.
        value = materialize(0, _Target(self._target_field), self._result)
        self._on_save(value)
        self.accept()

    def _build_hub(self, show_warning: Callable[[str], None]) -> Any:
        from omnia.core.providers import ProviderHub

        try:
            return ProviderHub(self._repo.llm_settings(), self._repo.tts_settings())
        except Exception as exc:  # boundary: surface bad provider config to the user
            logger.exception("smart_notes: could not build provider hub")
            show_warning(f"Omnia: provider config error:\n{exc}")
            return None


class _Target:
    """Minimal stand-in carrying ``target_field`` for :func:`materialize` (no rule needed)."""

    def __init__(self, target_field: str) -> None:
        self.target_field = target_field
