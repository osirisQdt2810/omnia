"""The note-type config table controller: load, base-field, field-create, save, cancel.

Owns the per-note-type table the dialog edits — picking the note type, designating its base
(input) field + decks, rendering the per-field rows, and persisting the merged config + the
global option flags into the synced store. The pure row↔config mapping lives in ``html.py``;
this is the thin ``pycmd`` glue over it. Only loaded inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from omnia import addon_user_files_dir
from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers import available_llm_providers
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.gui.smart_notes.html import (
    cycle_error_for_config,
    graph_payload,
    load_payload,
    merge_note_type_into,
    note_type_config_from_payload,
    row_to_payload,
    rows_for_note_type,
)
from omnia.plugins.smart_notes.integration.installer import (
    ClipperInstaller,
    InstallError,
    SubprocessCommandRunner,
)
from omnia.plugins.smart_notes.integration.integrations import (
    INTEGRATIONS,
    integration_for_key,
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
            "install_integration": self.on_install_integration,
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
                note_type_config_from_payload(
                    note_type,
                    base_field,
                    row_payloads,
                    # Carry the saved pinned-node positions through (as load_payload does), so
                    # switching the base field / adding a field doesn't reset node_positions={}
                    # and lose every pinned graph position on the next save.
                    positions=config.node_positions if config else {},
                )
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
            positions=dict(data.get("positions", {})),
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
                    # Merge so integration keys not sent by this page are preserved.
                    "auto_generate_integrations": {
                        **settings.auto_generate_integrations,
                        **(opts.get("auto_generate_integrations") or {}),
                    },
                }
            )
        )
        return {"ok": True}

    def on_cancel(self, _data: dict[str, Any]) -> None:
        # The shell owns the QDialog; reject() lives there, wired in at construction.
        self._reject()

    # -- One-click integration install (Integrations tab) -------------------------------------

    def on_install_integration(self, data: dict[str, Any]) -> dict[str, Any]:
        """Start a one-click install of the integration ``key`` OFF the Qt main thread.

        Cloning + a PyInstaller build (desktop) is a multi-minute, hundreds-of-MB job, so it runs
        via :func:`anki_compat.run_in_background`, pushing step-by-step progress and the final
        outcome to the page through ``window.__snClipperInstall*`` (mirrors the native-runtime
        installer). Returns immediately with ``{"started": True}``.
        """
        key = str(data.get("key", ""))
        integration = integration_for_key(key)
        if integration is None or not integration.install_kind:
            return {"started": False, "error": "This integration can't be installed."}

        installer = ClipperInstaller(
            clones_dir=addon_user_files_dir() / "clippers",
            host_python=self._ctx.native_manager.host_python(min_python=(3, 10)),
            runner=SubprocessCommandRunner(),
        )

        def op() -> None:
            installer.install(
                integration, lambda msg: self._push_install_progress(key, msg)
            )

        anki_compat.run_in_background(
            op,
            on_success=lambda _none: self._push_install_done(key, ok=True),
            on_failure=lambda exc: self._push_install_done(
                key, ok=False, error=self._install_error_text(exc)
            ),
            label=f"Omnia: installing {integration.name}…",
        )
        return {"started": True}

    @staticmethod
    def _install_error_text(exc: Exception) -> str:
        """A user-facing one-liner for a failed install (full detail goes to the log)."""
        logger.exception("smart_notes: clipper install failed")
        return str(exc) if isinstance(exc, InstallError) else "Install failed — see the log."

    def _push_install_progress(self, key: str, message: str) -> None:
        """Send an install progress line to ``window.__snClipperInstallProgress`` (main thread).

        MUST marshal to the Qt main thread: this runs on the ``run_in_background`` worker thread,
        and touching the WebView off-thread hard-crashes Qt (see the native-runtime controller).
        """
        anki_compat.run_on_main(
            lambda: self._ctx.eval_js(
                f"window.__snClipperInstallProgress({json.dumps(key)}, {json.dumps(message)});"
            )
        )

    def _push_install_done(self, key: str, *, ok: bool, error: str = "") -> None:
        """Send the install outcome to ``window.__snClipperInstallDone`` (already on main)."""
        result: dict[str, Any] = {"ok": ok} if ok else {"ok": False, "error": error}
        self._ctx.eval_js(
            f"window.__snClipperInstallDone({json.dumps(key)}, {json.dumps(result)});"
        )

    def _options_payload(self) -> dict[str, Any]:
        """The global Smart Notes option flags for the Options modal."""
        settings = self._ctx.settings()
        return {
            "generate_at_review": settings.generate_at_review,
            "regenerate_when_batching": settings.regenerate_when_batching,
            "allow_empty_fields": settings.allow_empty_fields,
            "auto_generate_integrations": settings.auto_generate_integrations,
            "integration_status": self._integration_status(),
            # The registered integrations (key + display text), so the Integrations tab renders
            # one row per entry instead of hardcoding a single clipper — adding an Integration to
            # the INTEGRATIONS tuple now shows up in the UI automatically.
            "integrations": [
                {
                    "key": integration.key,
                    "name": integration.name,
                    "description": integration.description,
                    "install_kind": integration.install_kind,
                }
                for integration in INTEGRATIONS
            ],
        }

    @staticmethod
    def _integration_status() -> dict[str, int]:
        """Per-integration counts of notes already carrying that integration's source tag.

        Best-effort — a collection-read failure yields 0 rather than crashing the dialog, so the
        "Detected N cards" status line never breaks the Options modal.
        """
        status: dict[str, int] = {}
        for integration in INTEGRATIONS:
            try:
                status[integration.key] = len(
                    anki_compat.find_note_ids(f"tag:{integration.source_tag}")
                )
            except Exception:  # status is cosmetic; never break the Options modal
                logger.exception(
                    "smart_notes: could not count notes for integration %s",
                    integration.key,
                )
                status[integration.key] = 0
        return status
