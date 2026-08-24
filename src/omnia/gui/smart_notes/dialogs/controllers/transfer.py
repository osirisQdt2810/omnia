"""The Export / Import buttons: moving one note type's setup between collections.

Thin Qt glue over :mod:`omnia.plugins.smart_notes.transfer`. Everything that decides anything
— what a bundle contains, how fields map, what an import would do — lives there and is pure;
this owns the file dialogs, the collection handle, and the shape of what the page is told.

Import is two round trips on purpose. The first reads the file and answers "here is what is
in it and what would happen"; the page then shows the collision choice (clone under a new
name, or overwrite with a field mapping) and sends back a decision. Nothing is written until
that second call, because an import can rewrite prompts and drop rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnia import addon_user_files_dir
from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine.tools.user_tools import (
    USER_TOOL_PREFIX,
    UserToolLoader,
    UserToolStore,
    risky_operations,
)
from omnia.plugins.smart_notes.transfer.bundle import BundleError, parse_bundle
from omnia.plugins.smart_notes.transfer.collection import (
    MODE_CLONE,
    MODE_CREATE,
    MODE_OVERWRITE,
    TransferError,
    apply_bundle,
    build_bundle,
    plan_import,
)
from omnia.plugins.smart_notes.transfer.remap import suggest_renames

logger = get_logger("smart_notes")

#: Suffix for a bundle file. Distinctive enough that a user's Downloads folder sorts them
#: together and Anki's own ``.apkg`` is never confused for one.
BUNDLE_SUFFIX = ".omnia-notetype.json"


class TransferController:
    """Handles the Export / Import ops the Smart Notes footer buttons send."""

    def __init__(self, ctx: Any, tool_loader: Optional[UserToolLoader] = None) -> None:
        """Initialise the controller.

        Args:
            ctx: The shared dialog context.
            tool_loader: Reads, writes AND REGISTERS ``user:`` tools. A loader rather than a
                bare store because an imported tool has to be registered before the remap
                reads it — see ``apply_bundle``. Injected by the smoke harness so a test run
                never writes Python into the source tree; defaults to the add-on's own
                ``user_files/tools`` (mirroring ``UserToolsController``).
        """
        self._ctx = ctx
        self._loader = tool_loader
        self._pending: Optional[Any] = None  # the bundle awaiting a collision decision

    def ops(self) -> dict[str, Any]:
        return {
            "export_note_type": self.on_export,
            "read_import_file": self.on_read_import_file,
            "apply_import": self.on_apply_import,
            "cancel_import": self.on_cancel_import,
        }

    # -- shared -------------------------------------------------------------------------
    def _collection(self) -> Any:
        return anki_compat.main_window().col

    def _tool_loader(self) -> UserToolLoader:
        if self._loader is None:
            self._loader = UserToolLoader(
                UserToolStore(addon_user_files_dir() / "tools")
            )
        return self._loader

    def _profile_name(self) -> str:
        try:
            return str(anki_compat.main_window().pm.name or "")
        except Exception:
            return ""

    # -- export -------------------------------------------------------------------------
    def on_export(self, data: dict[str, Any]) -> dict[str, Any]:
        """Write the selected note type's setup to a file the user picks."""
        note_type = str(data.get("note_type", "")).strip()
        if not note_type:
            return {"ok": False, "error": "Pick a note type first."}
        try:
            bundle = build_bundle(
                self._collection(),
                note_type,
                tool_store=self._tool_loader().store,
                profile=self._profile_name(),
            )
        except TransferError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # boundary: a bad export must not take the dialog down
            logger.exception("smart_notes: export failed for %r", note_type)
            return {"ok": False, "error": f"Could not read that note type: {exc}"}

        suggested = _safe_filename(note_type) + BUNDLE_SUFFIX
        path = self._ask_save_path(suggested)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            Path(path).write_text(bundle.to_json(), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"Could not write {path}: {exc}"}

        missing = bundle.missing_user_tools()
        return {
            "ok": True,
            "path": path,
            "note_type": note_type,
            "fields": len(bundle.smart_notes.fields),
            "tools": sorted(bundle.user_tools),
            "missing_tools": missing,
        }

    def on_cancel_import(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Drop the parsed bundle when the user closes the modal.

        Harmless to keep — the page gates on its own state — but a bundle carries tool
        SOURCE, and holding someone else's code in memory for the dialog's lifetime is not
        something to do by omission.
        """
        self._pending = None
        return {"ok": True}

    # -- import, step 1: read the file and describe what would happen ---------------------
    def on_read_import_file(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Let the user pick a bundle, then report what importing it would do."""
        path = self._ask_open_path()
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            bundle = parse_bundle(Path(path).read_text(encoding="utf-8"))
        except (BundleError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

        self._pending = bundle
        col = self._collection()
        existing = col.models.by_name(bundle.note_type_name)
        target_fields = (
            [str(f.get("name", "")) for f in existing.get("flds", [])]
            if existing
            else []
        )
        source_fields = bundle.field_names()
        return {
            "ok": True,
            "path": path,
            "note_type": bundle.note_type_name,
            "source": bundle.source.dict(),
            "collides": existing is not None,
            "source_fields": source_fields,
            "target_fields": target_fields,
            "suggested_renames": (
                suggest_renames(source_fields, target_fields) if existing else {}
            ),
            "rules": len(bundle.smart_notes.fields),
            "enabled": sum(1 for rule in bundle.smart_notes.fields if rule.enabled),
            "user_tools": sorted(bundle.user_tools),
            "carried_tools": self._carried_tools(bundle),
            "missing_tools": bundle.missing_user_tools(),
            "note_type_names": sorted(anki_compat.note_type_names(col)),
        }

    def _carried_tools(self, bundle: Any) -> list[dict[str, Any]]:
        """Describe each ``user:`` tool the bundle carries, for the approval list.

        The page shows the SOURCE and what it reaches for, because installing one runs it and
        the add-on's safety boundary for user tools is that read-and-run review — the import
        allowlist stopped being it once it had to permit ``os`` and ``subprocess``.
        """
        installed = set(self._tool_loader().store.slugs())
        described: list[dict[str, Any]] = []
        for name in bundle.required_user_tools():
            code = bundle.user_tools.get(name)
            if code is None:
                continue
            slug = name[len(USER_TOOL_PREFIX) :]
            described.append(
                {
                    "name": name,
                    "slug": slug,
                    "code": code,
                    "risks": risky_operations(code),
                    # An already-installed slug is never overwritten, so there is nothing to
                    # approve: what runs is the copy this machine's owner already reviewed.
                    "already_installed": slug in installed,
                }
            )
        return described

    # -- import, step 2: carry out the decision ------------------------------------------
    def on_apply_import(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply the pending bundle with the mode and mapping the user chose."""
        bundle = self._pending
        if bundle is None:
            return {"ok": False, "error": "Nothing to import — pick a file first."}

        mode = str(data.get("mode", "")) or MODE_CREATE
        if mode not in (MODE_CREATE, MODE_CLONE, MODE_OVERWRITE):
            return {"ok": False, "error": f"Unknown import mode {mode!r}."}
        target = str(data.get("target_name", "")).strip() or bundle.note_type_name
        # Absent means "decide for me"; an EMPTY object means "none of them" — the user
        # set every row to "not imported". Collapsing the two with ``or None`` would
        # import everything they just declined.
        raw_renames = data.get("renames")
        renames = (
            None
            if raw_renames is None
            else {str(k): str(v) for k, v in dict(raw_renames).items() if v}
        )

        approved = [str(name) for name in (data.get("approved_tools") or [])]
        col = self._collection()
        loader = self._tool_loader()
        try:
            plan = plan_import(
                col,
                bundle,
                mode=mode,
                target_name=target,
                renames=renames,
                tool_loader=loader,
                approved_tools=approved,
            )
            result = apply_bundle(col, bundle, plan, tool_loader=loader)
        except TransferError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # boundary: never take the dialog down on a bad bundle
            logger.exception("smart_notes: import failed for %r", target)
            return {"ok": False, "error": f"Import failed: {exc}"}

        self._pending = None
        report = result.remap
        return {
            "ok": True,
            "note_type": result.note_type,
            "created": result.created_note_type,
            "fields": result.fields_configured,
            "tools": result.tools_written,
            "tools_failed": result.tools_failed,
            "warnings": plan.warnings,
            "unapproved_tools": plan.unapproved_tools,
            "dropped_fields": list(report.dropped_fields) if report else [],
            "dropped_dependencies": list(report.dropped_dependencies) if report else [],
            "unchecked_tool_params": (
                list(report.unchecked_tool_params) if report else []
            ),
            "dropped_tool_params": (list(report.dropped_tool_params) if report else []),
        }

    # -- file dialogs --------------------------------------------------------------------
    def _ask_save_path(self, suggested: str) -> str:
        from aqt.qt import QFileDialog

        parent = self._ctx.parent_widget()
        path, _filter = QFileDialog.getSaveFileName(
            parent,
            "Export Smart Notes setup",
            str(Path.home() / suggested),
            "Omnia note type (*.json)",
        )
        return path or ""

    def _ask_open_path(self) -> str:
        from aqt.qt import QFileDialog

        parent = self._ctx.parent_widget()
        path, _filter = QFileDialog.getOpenFileName(
            parent,
            "Import a Smart Notes setup",
            str(Path.home()),
            "Omnia note type (*.json);;All files (*)",
        )
        return path or ""


def _safe_filename(name: str) -> str:
    """Make ``name`` safe as a file name on every platform Omnia ships to."""
    cleaned = "".join("-" if ch in '<>:"/\\|?*' else ch for ch in name).strip(" .")
    return cleaned or "note-type"
