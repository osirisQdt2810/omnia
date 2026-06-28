"""The Smart Notes config dialog — a polished webview table over the note-type model.

A :class:`~omnia.gui.web_dialog.WebDialog`-hosted page (gradient header, rounded card, a
scrolling per-field table, light/dark) that edits ONE note type's
:class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` at a time: pick the note type,
designate its base (input) field, then for every other field set whether to generate it (On),
whether to freeze it (Lock — blurs + protects from Auto-smart/Improve), its type
(text/image/tts), a prompt (edited in a popup, not inline), a kind-aware provider + model/voice
dropdown (model for text/image, voice for sound), and an overwrite flag.

Three off-thread LLM actions push their result back through page hooks: ✨ Auto-smart writes a
prompt+type for the On + unlocked fields; ✨ Improve (per field, and the global "Improve all")
rewrites a rough prompt into a polished one; ▶ Preview generates a sample for one field on a
random note. The provider/model/voice options come from a baked catalog
(:func:`~omnia.core.providers.catalog.catalog_payload`).

The page markup and the pure row↔config mapping live in ``html.py``; this class is the thin
Qt/Anki glue handling the ``pycmd`` ops and persisting through the config repo. Only loaded
inside Anki.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from aqt.theme import theme_manager

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers import ProviderError, ProviderHub, available_llm_providers
from omnia.core.providers.catalog import catalog_payload
from omnia.gui.smart_notes.html import (
    build_smart_notes_html,
    load_payload,
    merge_note_type_into,
    note_type_config_from_payload,
    row_to_payload,
    rows_for_note_type,
)
from omnia.gui.web_dialog import WebDialog
from omnia.plugins.smart_notes.integration import SmartNotesStore

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.plugins.smart_notes.config import SmartNotesNoteTypeConfig
    from omnia.plugins.smart_notes.engine import GenerationResult


class SmartNotesDialog(WebDialog):
    """Per-note-type Smart Notes table: base field + per-field generation config + Auto-smart."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        self._repo = repo
        # Per-note-type rules persist in the collection (synced); provider config (llm/tts)
        # stays in the TOML config via self._repo (see _build_hub).
        self._store = SmartNotesStore()
        self._log = get_logger("smart_notes")
        super().__init__(
            parent,
            title="Smart Notes ✨",
            html=build_smart_notes_html(
                dark=theme_manager.night_mode,
                init=self._initial_state(),
                catalog=catalog_payload(),
            ),
            handlers={
                "list_note_types": self._on_list_note_types,
                "load": self._on_load,
                "set_base_field": self._on_set_base_field,
                "create_field": self._on_create_field,
                "auto_smart": self._on_auto_smart,
                "improve_prompt": self._on_improve_prompt,
                "improve_all": self._on_improve_all,
                "preview": self._on_preview,
                "save": self._on_save,
                "cancel": self._on_cancel,
            },
            width=1040,
            height=640,
        )

    def _initial_state(self) -> dict[str, Any]:
        """The data baked into the page so it renders populated without an init pycmd.

        Seeds the note-type list + the first note type's load payload (base field, fields,
        rows, providers). The init pycmd callback is unreliable (the bridge channel isn't ready
        when the page's inline script first runs), so the first paint must not depend on it.
        """
        note_types = anki_compat.note_type_names()
        if not note_types:
            return {"note_types": []}
        first = note_types[0]
        payload = load_payload(
            first,
            self._settings().note_type_config(first),
            anki_compat.note_type_field_names(first),
            available_llm_providers(),
            all_decks=self._all_decks(),
        )
        return {"note_types": note_types, **payload}

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
            all_decks=self._all_decks(),
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
            "decks": list(config.decks) if config else [],
            "all_decks": self._all_decks(),
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
        finishes (success or a friendly ProviderError message). Reports a clear, actionable
        message when there is nothing to fill (no On + unlocked field) instead of silently
        succeeding.
        """
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )

        from omnia.plugins.smart_notes.authoring import PromptAuthor, candidate_fields

        candidates = candidate_fields(config)
        if not candidates:
            self._push_auto_result(
                error="Nothing to fill — turn On (and unlock) at least one field, then run "
                "Auto-smart."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_auto_result(error="Provider config error — see logs.")
            return

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).auto_smart(config),
            on_success=lambda updated: self._push_auto_result(
                config=updated, filled=len(candidates)
            ),
            on_failure=self._on_auto_failure,
            label="Omnia: auto-smart…",
        )

    def _on_improve_prompt(self, data: dict[str, Any]) -> None:
        """Rewrite ONE field's rough prompt into a polished one (off-thread; pushed back).

        Mechanism X: the user types a short/rough request and clicks ✨ Improve in the prompt
        editor; the result returns through ``window.__snImproveResult``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        rough = str(data.get("prompt", ""))
        if not rough.strip():
            self._push_improve(
                field, error="Type a rough prompt first, then Improve it."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_improve(field, error="Provider config error — see logs.")
            return
        other_fields = [
            name
            for name in anki_compat.note_type_field_names(note_type)
            if name != field
        ]

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve(
                note_type=note_type,
                base_field=base_field,
                target_field=field,
                rough=rough,
                other_fields=other_fields,
            ),
            on_success=lambda text: self._push_improve(field, prompt=text),
            on_failure=lambda exc: self._push_improve(
                field, error=self._friendly(exc, "Improve failed")
            ),
            label="Omnia: improving prompt…",
        )

    def _on_improve_all(self, data: dict[str, Any]) -> None:
        """Rewrite EVERY On + unlocked field's rough prompt at once (off-thread; pushed back).

        The result returns through ``window.__snImproveAllResult`` as ``{field: prompt}``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        items = [
            (str(row.get("field", "")), str(row.get("prompt", "")))
            for row in data.get("rows", [])
            if row.get("enabled")
            and not row.get("prompt_locked")
            and str(row.get("field", "")) != base_field
            and str(row.get("prompt", "")).strip()
        ]
        if not items:
            self._push_improve_all(
                error="No On + unlocked field has a prompt to improve."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_improve_all(error="Provider config error — see logs.")
            return

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve_all(
                note_type=note_type, base_field=base_field, items=items
            ),
            on_success=lambda mapping: self._push_improve_all(improved=mapping),
            on_failure=lambda exc: self._push_improve_all(
                error=self._friendly(exc, "Improve all failed")
            ),
            label="Omnia: improving prompts…",
        )

    def _on_preview(self, data: dict[str, Any]) -> None:
        """Generate a sample for one field against a real (or fabricated) note (off-thread).

        Lets the user test a prompt before saving; the result returns through
        ``window.__snPreviewResult``.
        """
        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine import GenerationService

        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        kind = str(data.get("type", "text"))
        prompt = str(data.get("prompt", ""))
        rule = SmartNotesFieldRule(
            note_type=note_type,
            # Mirror compile_note_type_rules: with no prompt the base field is the source.
            source_field="" if prompt else base_field,
            target_field=field,
            kind=kind,
            prompt=prompt,
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            voice=str(data.get("voice", "")),
            language=str(data.get("language", "")),
        )
        fields = self._preview_fields(note_type, base_field)
        hub = self._build_hub()
        if hub is None:
            self._push_preview(field, error="Provider config error — see logs.")
            return
        service = GenerationService(hub)

        anki_compat.run_in_background(
            lambda: service.generate(rule, fields),
            on_success=lambda result: self._push_preview(field, result=result),
            on_failure=lambda exc: self._push_preview(
                field, error=self._friendly(exc, "Preview failed")
            ),
            label="Omnia: preview…",
        )

    def _preview_fields(self, note_type: str, base_field: str) -> dict[str, str]:
        """The field values a preview runs against.

        Uses the FIRST existing note of ``note_type`` when there is one; otherwise fabricates a
        sample (all fields blank). Either way, a blank base field is seeded with a sample word —
        most prompts self-guard to output nothing for an empty base, which is exactly the
        "(empty result)" the preview was hitting, so the seed makes the preview meaningful.
        """
        note = anki_compat.random_note_of_type(note_type or None)
        if note is not None:
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        else:
            fields = {name: "" for name in anki_compat.note_type_field_names(note_type)}
        if base_field and not str(fields.get(base_field, "")).strip():
            fields[base_field] = "example"
        return fields

    def _on_save(self, data: dict[str, Any]) -> dict[str, Any]:
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )
        if not config.note_type:
            return {"error": "Pick a note type first."}
        # Per-note-type rules persist in the COLLECTION (synced), not the TOML config.
        settings = self._store.load()
        merged = merge_note_type_into(list(settings.note_types), config)
        self._store.save(settings.copy(update={"note_types": merged}))
        return {"ok": True}

    def _on_cancel(self, _data: dict[str, Any]) -> None:
        self.reject()

    # --- helpers ---------------------------------------------------------------------
    def _settings(self) -> Any:
        # Per-note-type rules persist in the collection (synced), not in the TOML config.
        return self._store.load()

    @staticmethod
    def _all_decks() -> list[dict[str, Any]]:
        """The full deck list for the picker as ``[{id, name}, ...]`` objects for the JS."""
        return [
            {"id": deck_id, "name": name} for deck_id, name in anki_compat.deck_names()
        ]

    def _build_hub(self) -> Optional[ProviderHub]:
        try:
            return ProviderHub(self._repo.llm_settings(), self._repo.tts_settings())
        except Exception:  # boundary: a bad provider config must not crash the dialog
            self._log.exception(
                "smart_notes: could not build provider hub for auto-smart"
            )
            return None

    def _on_auto_failure(self, exc: Exception) -> None:
        self._push_auto_result(error=self._friendly(exc, "Auto-smart failed"))

    def _friendly(self, exc: Exception, prefix: str) -> str:
        """A short user-facing message for ``exc`` (ProviderError verbatim; else log + generic)."""
        if isinstance(exc, ProviderError):
            return f"{prefix}: {exc}"
        self._log.exception("smart_notes: %s raised", prefix)
        return f"{prefix} — see logs."

    def _push_auto_result(
        self,
        *,
        config: Optional[SmartNotesNoteTypeConfig] = None,
        filled: int = 0,
        error: str = "",
    ) -> None:
        """Send the Auto-smart outcome to the page's ``window.__snAutoResult`` hook."""
        if error:
            result: dict[str, Any] = {"error": error}
        else:
            assert config is not None
            result = {
                "rows": [row_to_payload(row) for row in config.fields],
                "filled": filled,
            }
        self.eval_js(f"window.__snAutoResult({json.dumps(result)});")

    def _push_improve(self, field: str, *, prompt: str = "", error: str = "") -> None:
        """Send one Improve outcome to the page's ``window.__snImproveResult`` hook."""
        result: dict[str, Any] = {"error": error} if error else {"prompt": prompt}
        self.eval_js(
            f"window.__snImproveResult({json.dumps(field)}, {json.dumps(result)});"
        )

    def _push_improve_all(
        self, *, improved: Optional[dict[str, str]] = None, error: str = ""
    ) -> None:
        """Send the Improve-all outcome to the page's ``window.__snImproveAllResult`` hook."""
        result: dict[str, Any] = (
            {"error": error} if error else {"improved": improved or {}}
        )
        self.eval_js(f"window.__snImproveAllResult({json.dumps(result)});")

    def _push_preview(
        self,
        field: str,
        *,
        result: Optional[GenerationResult] = None,
        error: str = "",
    ) -> None:
        """Send a Preview outcome to the page's ``window.__snPreviewResult`` hook.

        Text previews carry the rendered HTML; audio is played here and reported as a note;
        an image is reported as generated (not inserted — this is only a preview).
        """
        if error:
            payload: dict[str, Any] = {"error": error}
        elif result is None:
            payload = {"error": "Preview produced no result."}
        elif result.kind == "text":
            payload = {"kind": "text", "text": result.text or ""}
        elif result.kind == "tts":
            anki_compat.play_audio(result.data or b"", result.ext)
            payload = {"kind": "tts", "message": "Audio preview played."}
        else:
            payload = {"kind": "image", "message": "Image generated (preview only)."}
        self.eval_js(
            f"window.__snPreviewResult({json.dumps(field)}, {json.dumps(payload)});"
        )
