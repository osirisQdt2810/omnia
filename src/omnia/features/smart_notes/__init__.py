"""Smart Notes feature: generate note fields (text/image) and audio via an AI provider.

Adds a Browser context-menu action that runs the configured field rules on the selected
notes. Generation (network) runs off the Qt main thread via the threading seam; results are
written back to the notes + media on the main thread. The pure generation logic lives in
``logic.py``; this module is the Anki glue.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.features.smart_notes.logic import GenerationResult, GenerationService

_HOOK = "browser_will_show_context_menu"


@register("smart_notes")
class SmartNotesPlugin(FeaturePlugin):
    """AI generation of note fields + audio, driven from the Browser."""

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
        anki_compat.subscribe_hook(_HOOK, self._on_browser_menu)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook(_HOOK, self._on_browser_menu)
        self._ctx = None
        self._service = None

    # --- Browser glue ---------------------------------------------------------------
    def _on_browser_menu(self, browser: Any, menu: Any) -> None:
        from aqt.qt import QAction

        action = QAction("Omnia: Generate fields", menu)
        action.triggered.connect(lambda: self._generate(browser))
        menu.addAction(action)

    def _generate(self, browser: Any) -> None:
        from aqt.utils import tooltip

        rules = list(self._ctx.settings.fields) if self._ctx else []
        if not rules:
            tooltip("Omnia: configure smart_notes field rules first (Tools → Omnia).")
            return
        plan = self._build_plan(browser.selectedNotes(), rules)
        if not plan:
            tooltip("Omnia: no matching field rules for the selected notes.")
            return

        service = self._service
        assert service is not None

        def op() -> list[tuple[int, Any, GenerationResult]]:
            return [
                (nid, rule, service.generate(rule, fields))
                for nid, rule, fields in plan
            ]

        anki_compat.run_in_background(
            op,
            on_success=self._apply,
            on_failure=self._on_error,
            label="Omnia: generating…",
        )

    def _build_plan(
        self, note_ids: list[int], rules: list[Any]
    ) -> list[tuple[int, Any, dict[str, str]]]:
        """Read note inputs on the main thread (the background op must not touch the col)."""
        col = anki_compat.main_window().col
        plan: list[tuple[int, Any, dict[str, str]]] = []
        for nid in note_ids:
            note = col.get_note(nid)
            # .keys() is Anki's Note API for field names (Note isn't dict-iterable).
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
            type_name = _note_type_name(note)
            for rule in rules:
                if rule.note_type and rule.note_type != type_name:
                    continue
                if rule.target_field not in fields:
                    continue
                plan.append((nid, rule, fields))
        return plan

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
