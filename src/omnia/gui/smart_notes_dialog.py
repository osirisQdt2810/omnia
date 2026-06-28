"""The Smart Notes config dialog — a polished webview table over the note-type model.

A :class:`~omnia.gui.web_dialog.WebDialog`-hosted page (gradient header, rounded card, a
scrolling per-field table, light/dark) that edits ONE note type's
:class:`~omnia.core.config.models.SmartNotesNoteTypeConfig` at a time: pick the note type,
designate its base (input) field, then for every other field set whether to generate it, its
type (text/tts/image), a prompt template, a lock to protect the prompt from Auto-smart,
optional provider/model overrides, and an overwrite flag. The ✨ Auto-smart button asks the
LLM (off the Qt main thread) to fill in prompts/types for the enabled, unlocked fields.

The page markup and the pure row↔config mapping live in ``smart_notes_html.py``; this class
is the thin Qt/Anki glue handling the ``pycmd`` ops and persisting through the config repo.
Only loaded inside Anki.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from aqt.theme import theme_manager

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers import ProviderError, ProviderHub, available_llm_providers
from omnia.gui.smart_notes_html import (
    build_smart_notes_html,
    load_payload,
    merge_note_type_into,
    note_type_config_from_payload,
    row_to_payload,
    rows_for_note_type,
)
from omnia.gui.web_dialog import WebDialog

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.core.config.models import SmartNotesNoteTypeConfig


class SmartNotesDialog(WebDialog):
    """Per-note-type Smart Notes table: base field + per-field generation config + Auto-smart."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        self._repo = repo
        self._log = get_logger("smart_notes")
        super().__init__(
            parent,
            title="Smart Notes ✨",
            html=build_smart_notes_html(dark=theme_manager.night_mode),
            handlers={
                "list_note_types": self._on_list_note_types,
                "load": self._on_load,
                "set_base_field": self._on_set_base_field,
                "create_field": self._on_create_field,
                "auto_smart": self._on_auto_smart,
                "save": self._on_save,
                "cancel": self._on_cancel,
            },
            width=920,
            height=620,
        )

    # --- pycmd handlers --------------------------------------------------------------
    def _on_list_note_types(self, _data: dict[str, Any]) -> list[str]:
        return anki_compat.note_type_names()

    def _on_load(self, data: dict[str, Any]) -> dict[str, Any]:
        note_type = str(data.get("note_type", ""))
        return load_payload(
            note_type,
            self._settings().note_type_config(note_type),
            anki_compat.note_type_field_names(note_type),
            available_llm_providers(),
        )

    def _on_set_base_field(self, data: dict[str, Any]) -> dict[str, Any]:
        # Re-render the rows for the chosen base field, keeping any saved config for the rest.
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        config = self._settings().note_type_config(note_type)
        all_fields = anki_compat.note_type_field_names(note_type)
        rows = rows_for_note_type(config, all_fields, base_field)
        return {
            "note_type": note_type,
            "base_field": base_field,
            "all_fields": all_fields,
            "rows": [row_to_payload(row) for row in rows],
            "providers": available_llm_providers(),
        }

    def _on_create_field(self, data: dict[str, Any]) -> dict[str, Any]:
        note_type = str(data.get("note_type", ""))
        field_name = str(data.get("field_name", "")).strip()
        if not field_name:
            return {"error": "Field name cannot be empty."}
        # Boundary: surface a schema-change failure to the page instead of crashing the dialog.
        try:
            all_fields = anki_compat.add_note_type_field(note_type, field_name)
        except Exception as exc:
            self._log.exception("smart_notes: failed to add field %r", field_name)
            return {"error": f"Could not add the field: {exc}"}
        return {"all_fields": all_fields}

    def _on_auto_smart(self, data: dict[str, Any]) -> None:
        """Run Auto-smart off the Qt main thread; push the result back via the page hook.

        Returns None immediately — the LLM call can't block the main thread, so the new rows
        are delivered to the page through ``window.__snAutoResult`` once the background op
        finishes (success or a friendly ProviderError message).
        """
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
        )
        hub = self._build_hub()
        if hub is None:
            self._push_auto_result(error="Provider config error — see logs.")
            return

        from omnia.features.smart_notes.auto_smart import generate_auto_smart

        anki_compat.run_in_background(
            lambda: generate_auto_smart(hub, config),
            on_success=lambda updated: self._push_auto_result(config=updated),
            on_failure=self._on_auto_failure,
            label="Omnia: auto-smart…",
        )

    def _on_save(self, data: dict[str, Any]) -> dict[str, Any]:
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
        )
        if not config.note_type:
            return {"error": "Pick a note type first."}
        merged = merge_note_type_into(list(self._settings().note_types), config)
        self._repo.update_section(
            "smart_notes",
            {"note_types": [nt.model_dump() for nt in merged]},
        )
        return {"ok": True}

    def _on_cancel(self, _data: dict[str, Any]) -> None:
        self.reject()

    # --- helpers ---------------------------------------------------------------------
    def _settings(self) -> Any:
        return self._repo.config.smart_notes

    def _build_hub(self) -> Optional[ProviderHub]:
        try:
            return ProviderHub(self._repo.llm_settings(), self._repo.tts_settings())
        except Exception:  # boundary: a bad provider config must not crash the dialog
            self._log.exception(
                "smart_notes: could not build provider hub for auto-smart"
            )
            return None

    def _on_auto_failure(self, exc: Exception) -> None:
        if isinstance(exc, ProviderError):
            self._push_auto_result(error=f"Auto-smart failed: {exc}")
        else:
            self._log.exception("smart_notes: auto-smart raised")
            self._push_auto_result(error="Auto-smart failed — see logs.")

    def _push_auto_result(
        self,
        *,
        config: Optional[SmartNotesNoteTypeConfig] = None,
        error: str = "",
    ) -> None:
        """Send the Auto-smart outcome to the page's ``window.__snAutoResult`` hook."""
        import json

        if error:
            result: dict[str, Any] = {"error": error}
        else:
            assert config is not None
            result = {"rows": [row_to_payload(row) for row in config.fields]}
        self.eval_js(f"window.__snAutoResult({json.dumps(result)});")
