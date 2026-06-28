"""Smart Notes feature: generate note fields (text/image) and audio via an AI provider.

Two entry points share one generation core: a Browser context-menu action (over the selected
notes) and an editor ✨ button (over the note open in the editor). Generation (network) runs
off the Qt main thread via the threading seam; results are written back to the notes + media
on the main thread. The pure logic lives in ``logic.py``; this module is the Anki glue.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.features.smart_notes.logic import (
    GenerationResult,
    GenerationService,
    build_generation_plan,
)

_BROWSER_HOOK = "browser_will_show_context_menu"
_EDITOR_HOOK = "editor_did_init_buttons"


@register("smart_notes")
class SmartNotesPlugin(FeaturePlugin):
    """AI generation of note fields + audio, driven from the Browser and the editor."""

    name = "Smart Notes"
    description = (
        "Generate note fields (text/image) and audio (TTS) with an AI provider."
    )
    order = 50

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._service: Optional[GenerationService] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._service = GenerationService(ctx.providers)
        anki_compat.subscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.subscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.unsubscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)
        self._ctx = None
        self._service = None

    # --- bespoke settings dialog -----------------------------------------------------
    def custom_config_dialog(self, repo: Any, parent: Any) -> Optional[Any]:
        from omnia.gui.smart_notes_dialog import SmartNotesDialog

        return SmartNotesDialog(repo, parent)

    # --- Browser glue ----------------------------------------------------------------
    def _on_browser_menu(self, browser: Any, menu: Any) -> None:
        from aqt.qt import QAction

        action = QAction("Omnia: Generate fields", menu)
        action.triggered.connect(lambda: self._generate_for_browser(browser))
        menu.addAction(action)

    def _generate_for_browser(self, browser: Any) -> None:
        from aqt.utils import tooltip

        rules = self._rules()
        if not rules:
            tooltip("Omnia: configure smart_notes field rules first (Tools → Omnia).")
            return
        col = anki_compat.main_window().col
        plan = self._plan_for_notes(
            [col.get_note(nid) for nid in browser.selectedNotes()], rules
        )
        if not plan:
            tooltip("Omnia: no matching field rules for the selected notes.")
            return
        self._run(plan, self._apply)

    # --- editor glue -----------------------------------------------------------------
    def _on_editor_buttons(self, buttons: list[Any], editor: Any) -> None:
        from omnia.features.smart_notes.editor import add_generate_button

        add_generate_button(buttons, editor, self._generate_for_editor)

    def _generate_for_editor(self, editor: Any) -> None:
        from aqt.utils import tooltip

        note = getattr(editor, "note", None)
        rules = self._rules()
        if note is None or not rules:
            tooltip(
                "Omnia: open a note and configure field rules first (Tools → Omnia)."
            )
            return
        plan = self._plan_for_notes([note], rules)
        if not plan:
            tooltip("Omnia: no matching field rules for this note.")
            return

        def on_success(results: list[tuple[int, Any, GenerationResult]]) -> None:
            self._apply_to_editor(editor, results)

        self._run(plan, on_success)

    # --- shared generation core ------------------------------------------------------
    def _rules(self) -> list[Any]:
        return list(self._ctx.settings.fields) if self._ctx else []

    def _plan_for_notes(
        self, notes: list[Any], rules: list[Any]
    ) -> list[tuple[int, Any, dict[str, str]]]:
        """Build the (note id, rule, fields) plan for ``notes`` (read on the main thread).

        The background op must not touch the collection, so note inputs are read here and the
        per-note rule selection is delegated to the pure ``build_generation_plan``.
        """
        plan: list[tuple[int, Any, dict[str, str]]] = []
        for note in notes:
            # .keys() is Anki's Note API for field names (Note isn't dict-iterable).
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
            type_name = _note_type_name(note)
            for rule, note_fields in build_generation_plan(fields, type_name, rules):
                plan.append((note.id, rule, note_fields))
        return plan

    def _run(
        self,
        plan: list[tuple[int, Any, dict[str, str]]],
        on_success: Any,
    ) -> None:
        """Generate every plan entry off the Qt main thread, then hand results to ``on_success``."""
        service = self._service
        assert service is not None

        def op() -> list[tuple[int, Any, GenerationResult]]:
            return [
                (nid, rule, service.generate(rule, fields))
                for nid, rule, fields in plan
            ]

        anki_compat.run_in_background(
            op,
            on_success=on_success,
            on_failure=self._on_error,
            label="Omnia: generating…",
        )

    def _apply(self, results: list[tuple[int, Any, GenerationResult]]) -> None:
        """Write generated content back to the notes + media (main thread)."""
        from aqt.utils import showWarning, tooltip

        log = get_logger("smart_notes")
        col = anki_compat.main_window().col
        written = 0
        for nid, rule, result in results:
            try:
                note = col.get_note(nid)
                if rule.target_field not in note:
                    continue
                note[rule.target_field] = self._materialize(nid, rule, result)
                anki_compat.update_note(note)
                written += 1
            except Exception:  # one bad note must not abort the rest
                log.exception("smart_notes: failed to write note %s", nid)
        if written:
            tooltip(f"Omnia: generated {written} field(s).")
        elif results:
            showWarning(
                "Omnia: generation succeeded but no notes could be saved — see logs."
            )

    def _apply_to_editor(
        self, editor: Any, results: list[tuple[int, Any, GenerationResult]]
    ) -> None:
        """Write generated content into the note open in ``editor`` and refresh it (main thread).

        Media writes + note mutation happen here (the main thread); the editor's note object
        is mutated in place and reloaded so the new field values appear immediately.
        """
        from aqt.utils import showWarning, tooltip

        log = get_logger("smart_notes")
        note = getattr(editor, "note", None)
        if note is None:
            return
        written = 0
        for nid, rule, result in results:
            try:
                if rule.target_field not in note:
                    continue
                note[rule.target_field] = self._materialize(nid, rule, result)
                written += 1
            except Exception:  # one bad field must not abort the rest
                log.exception(
                    "smart_notes: failed to write field %s", rule.target_field
                )
        if written:
            anki_compat.update_note(note)
            self._reload_editor(editor)
            tooltip(f"Omnia: generated {written} field(s).")
        elif results:
            showWarning(
                "Omnia: generation succeeded but no fields could be written — see logs."
            )

    @staticmethod
    def _reload_editor(editor: Any) -> None:
        """Refresh the editor view across Anki versions (``loadNote`` / ``loadNoteKeepingFocus``)."""
        for attr in ("loadNoteKeepingFocus", "loadNote"):
            reload_view = getattr(editor, attr, None)
            if callable(reload_view):
                reload_view()
                return

    @staticmethod
    def _materialize(nid: int, rule: Any, result: GenerationResult) -> str:
        # Adding a new kind requires updating both GenerationService.generate and this
        # method; if a 4th kind appears, consider making GenerationResult a small ABC.
        if result.kind == "text":
            return result.text or ""
        filename = f"omnia-{nid}-{rule.target_field}.{result.ext}"
        stored = anki_compat.add_media_file(filename, result.data or b"")
        if result.kind == "image":
            return f'<img src="{stored}">'
        return f"[sound:{stored}]"  # tts

    @staticmethod
    def _on_error(exc: Exception) -> None:
        from aqt.utils import showWarning

        showWarning(f"Omnia smart_notes generation failed:\n{exc}")


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
