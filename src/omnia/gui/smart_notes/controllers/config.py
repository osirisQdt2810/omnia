"""The note-type config table controller: load, base-field, field-create, save, cancel.

Owns the per-note-type table the dialog edits — picking the note type, designating its base
(input) field + decks, rendering the per-field rows, and persisting the merged config + the
global option flags into the synced store. The pure row↔config mapping lives in ``html.py``;
this is the thin ``pycmd`` glue over it. Only loaded inside Anki.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers import available_llm_providers
from omnia.gui.smart_notes.context import SmartNotesContext
from omnia.gui.smart_notes.html import (
    cycle_error_for_config,
    graph_payload,
    load_payload,
    merge_note_type_into,
    note_type_config_from_payload,
    row_to_payload,
    rows_for_note_type,
)

logger = get_logger("smart_notes")


class ConfigController:
    """The note-type table + persistence ops.

    Args:
        ctx: The shared service context.
        reject: The shell's ``QDialog.reject`` (Cancel closes the dialog). Injected rather than
            placed on the context because only this controller's Cancel op needs it.
    """

    def __init__(self, ctx: SmartNotesContext, reject: Callable[[], None]) -> None:
        self._ctx = ctx
        self._reject = reject

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "list_note_types": self.on_list_note_types,
            "load": self.on_load,
            "set_base_field": self.on_set_base_field,
            "create_field": self.on_create_field,
            "save": self.on_save,
            "cancel": self.on_cancel,
        }

    def load_payload_for(self, note_type: str) -> dict[str, Any]:
        """The full load payload for ``note_type`` (base field, fields, rows, providers, options).

        Shared by the shell's initial-state bake and the ``load`` op so the first paint and a
        later note-type switch produce an identical shape.
        """
        payload = load_payload(
            note_type,
            self._ctx.settings().note_type_config(note_type),
            anki_compat.note_type_field_names(note_type),
            available_llm_providers(),
            all_decks=self._ctx.all_decks(),
        )
        payload["options"] = self._options_payload()
        return payload

    def on_list_note_types(self, _data: dict[str, Any]) -> list[str]:
        return anki_compat.note_type_names()

    def on_load(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.load_payload_for(str(data.get("note_type", "")))

    def on_set_base_field(self, data: dict[str, Any]) -> dict[str, Any]:
        # Re-render the rows for the chosen base field, keeping any saved config for the rest.
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        config = self._ctx.settings().note_type_config(note_type)
        all_fields = anki_compat.note_type_field_names(note_type)
        rows = rows_for_note_type(config, all_fields, base_field)
        row_payloads = [row_to_payload(row) for row in rows]
        return {
            "note_type": note_type,
            "base_field": base_field,
            "all_fields": all_fields,
            "rows": row_payloads,
            "providers": available_llm_providers(),
            "decks": list(config.decks) if config else [],
            "all_decks": self._ctx.all_decks(),
            "options": self._options_payload(),
            "graph": graph_payload(
                note_type_config_from_payload(note_type, base_field, row_payloads)
            ),
        }

    def on_create_field(self, data: dict[str, Any]) -> dict[str, Any]:
        note_type = str(data.get("note_type", ""))
        field_name = str(data.get("field_name", "")).strip()
        if not field_name:
            return {"error": "Field name cannot be empty."}
        # Boundary: surface a schema-change failure to the page instead of crashing the dialog.
        try:
            all_fields = anki_compat.add_note_type_field(note_type, field_name)
        except Exception as exc:
            logger.exception("smart_notes: failed to add field %r", field_name)
            return {"error": f"Could not add the field: {exc}"}
        return {"all_fields": all_fields}

    def on_save(self, data: dict[str, Any]) -> dict[str, Any]:
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )
        if not config.note_type:
            return {"error": "Pick a note type first."}
        # Persistence backstop (W2): never save a config whose field dependencies form a cycle
        # (the client cycle-precheck is complete given a fresh graph, but a stale graph / bug
        # must not slip a cyclic prompt+deps into a row).
        cycle = cycle_error_for_config(config)
        if cycle:
            return {"error": cycle}
        # Per-note-type rules persist in the COLLECTION (synced), not the TOML config. The
        # global option flags ride along on the same SmartNotesSettings.
        settings = self._ctx.store.load()
        merged = merge_note_type_into(list(settings.note_types), config)
        opts = dict(data.get("options", {}))
        self._ctx.store.save(
            settings.copy(
                update={
                    "note_types": merged,
                    "generate_at_review": bool(
                        opts.get("generate_at_review", settings.generate_at_review)
                    ),
                    "regenerate_when_batching": bool(
                        opts.get(
                            "regenerate_when_batching",
                            settings.regenerate_when_batching,
                        )
                    ),
                    "allow_empty_fields": bool(
                        opts.get("allow_empty_fields", settings.allow_empty_fields)
                    ),
                }
            )
        )
        return {"ok": True}

    def on_cancel(self, _data: dict[str, Any]) -> None:
        # The shell owns the QDialog; reject() lives there, wired in at construction.
        self._reject()

    def _options_payload(self) -> dict[str, Any]:
        """The global Smart Notes option flags for the Options modal."""
        settings = self._ctx.settings()
        return {
            "generate_at_review": settings.generate_at_review,
            "regenerate_when_batching": settings.regenerate_when_batching,
            "allow_empty_fields": settings.allow_empty_fields,
        }
