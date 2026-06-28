"""Smart Notes feature: generate note fields (text/image) and audio via an AI provider.

Entry points share one generation core: a Browser context-menu action (over the selected
notes), a deck/note-type sidebar batch, an editor ✨ button (over the open note), a per-field
right-click menu (one field, on demand, plus one-off custom prompts), and optional review-time
pre-generation. All generation runs through :meth:`GenerationService.generate_note` so chained
fields, skip rules, and Markdown conversion apply uniformly. Network runs off the Qt main
thread via the threading seam; results are written back to notes + media on the main thread.
The pure logic lives in ``logic.py``; this module + ``batch.py`` + ``review_evaluator.py`` are
the Anki glue.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.features.smart_notes.batch import BatchGenerator, BatchSummary, materialize
from omnia.features.smart_notes.editor import (
    add_generate_button,
    set_button_enabled,
)
from omnia.features.smart_notes.logic import (
    GenerationResult,
    GenerationService,
    select_rules_for_note,
)
from omnia.features.smart_notes.review_evaluator import ReviewTimeEvaluator

_BROWSER_HOOK = "browser_will_show_context_menu"
_SIDEBAR_HOOK = "browser_sidebar_will_show_context_menu"
_EDITOR_HOOK = "editor_did_init_buttons"
_EDITOR_MENU_HOOK = "editor_will_show_context_menu"
_REVIEW_HOOK = "reviewer_did_show_question"


@register("smart_notes")
class SmartNotesPlugin(FeaturePlugin):
    """AI generation of note fields + audio, driven from the Browser, sidebar, and editor."""

    name = "Smart Notes"
    description = (
        "Generate note fields (text/image) and audio (TTS) with an AI provider."
    )
    group = "AI"
    order = 50

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._service: Optional[GenerationService] = None
        self._review: Optional[ReviewTimeEvaluator] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._service = GenerationService(ctx.providers)
        self._review = ReviewTimeEvaluator(self._service, ctx.settings)
        anki_compat.subscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.subscribe_hook(_SIDEBAR_HOOK, self._on_sidebar_menu)
        anki_compat.subscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)
        anki_compat.subscribe_hook(_EDITOR_MENU_HOOK, self._on_editor_context_menu)
        anki_compat.subscribe_hook(_REVIEW_HOOK, self._on_review_question)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.unsubscribe_hook(_SIDEBAR_HOOK, self._on_sidebar_menu)
        anki_compat.unsubscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)
        anki_compat.unsubscribe_hook(_EDITOR_MENU_HOOK, self._on_editor_context_menu)
        anki_compat.unsubscribe_hook(_REVIEW_HOOK, self._on_review_question)
        self._ctx = None
        self._service = None
        self._review = None

    # --- bespoke settings dialog -----------------------------------------------------
    def custom_config_dialog(self, repo: Any, parent: Any) -> Optional[Any]:
        from omnia.gui.smart_notes_dialog import SmartNotesDialog

        return SmartNotesDialog(repo, parent)

    # --- Browser selection batch -----------------------------------------------------
    def _on_browser_menu(self, browser: Any, menu: Any) -> None:
        from aqt.qt import QAction

        action = QAction("✨ Generate Smart Fields", menu)
        action.triggered.connect(lambda: self._generate_for_browser(browser))
        menu.addSeparator()
        menu.addAction(action)

    def _generate_for_browser(self, browser: Any) -> None:
        from aqt.utils import tooltip

        if not self._settings().fields:
            tooltip("Omnia: configure smart_notes field rules first (Tools → Omnia).")
            return
        self._run_batch(list(browser.selectedNotes()))

    # --- deck / note-type sidebar batch ----------------------------------------------
    def _on_sidebar_menu(
        self, _tree_view: Any, menu: Any, sidebar_item: Any, *_a: Any
    ) -> None:
        # Anki fires this hook as (tree_view, menu, sidebar_item, index); accept the trailing
        # args defensively so a signature tweak across versions can't break the menu.
        from aqt.qt import QAction

        note_ids = _sidebar_note_ids(sidebar_item)
        if note_ids is None:
            return  # not a deck/note-type node
        action = QAction("✨ Generate Smart Fields", menu)
        action.triggered.connect(lambda: self._run_batch(note_ids))
        menu.addSeparator()
        menu.addAction(action)

    def _run_batch(self, note_ids: list[int]) -> None:
        from aqt.utils import tooltip

        service = self._service
        settings = self._settings()
        if service is None or not settings.fields or not note_ids:
            tooltip("Omnia: no matching field rules for the selection.")
            return
        BatchGenerator(service, settings).run(
            [int(nid) for nid in note_ids], self._on_batch_done
        )

    @staticmethod
    def _on_batch_done(summary: BatchSummary) -> None:
        from aqt.utils import tooltip

        tooltip(f"Omnia: {summary.message()}")

    # --- editor button ---------------------------------------------------------------
    def _on_editor_buttons(self, buttons: list[Any], editor: Any) -> None:
        add_generate_button(buttons, editor, self._generate_for_editor)

    def _generate_for_editor(self, editor: Any) -> None:
        from aqt.utils import tooltip

        note = getattr(editor, "note", None)
        if note is None or not self._settings().fields:
            tooltip(
                "Omnia: open a note and configure field rules first (Tools → Omnia)."
            )
            return
        rules = select_rules_for_note(
            list(self._settings().fields),
            _note_type_name(note),
            list(note.keys()),
        )
        if not rules:
            tooltip("Omnia: no matching field rules for this note.")
            return
        set_button_enabled(editor, False)
        self._generate_into_note(editor, note, rules)

    def _generate_into_note(self, editor: Any, note: Any, rules: list[Any]) -> None:
        """Generate ``rules`` for ``note`` off-thread, then write + reload the editor."""
        service = self._service
        assert service is not None
        settings = self._settings()
        fields = {name: note[name] for name in note.keys()}  # noqa: SIM118

        def op() -> list[tuple[Any, GenerationResult]]:
            return service.generate_note(
                rules,
                fields,
                allow_empty_fields=settings.allow_empty_fields,
                overwrite=settings.overwrite,
            )

        anki_compat.run_in_background(
            op,
            on_success=lambda results: self._apply_to_editor(editor, note, results),
            on_failure=lambda exc: self._on_editor_error(editor, exc),
            label="Omnia: generating…",
        )

    def _apply_to_editor(
        self, editor: Any, note: Any, results: list[tuple[Any, GenerationResult]]
    ) -> None:
        """Write generated content into the editor's note + refresh it (main thread)."""
        from aqt.utils import showWarning, tooltip

        set_button_enabled(editor, True)
        log = get_logger("smart_notes")
        written = 0
        for rule, result in results:
            try:
                if rule.target_field not in note:
                    continue
                note[rule.target_field] = materialize(
                    int(getattr(note, "id", 0)), rule, result
                )
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

    def _on_editor_error(self, editor: Any, exc: Exception) -> None:
        from aqt.utils import showWarning

        set_button_enabled(editor, True)
        showWarning(f"Omnia smart_notes generation failed:\n{exc}")

    # --- editor field right-click menu -----------------------------------------------
    def _on_editor_context_menu(self, editor_webview: Any, menu: Any) -> None:
        from omnia.features.smart_notes.field_menu import build_field_menu

        editor = getattr(editor_webview, "editor", editor_webview)
        build_field_menu(self._ctx, editor, menu, self._generate_field)

    def _generate_field(self, editor: Any, field: str) -> None:
        """Generate just the rule(s) targeting ``field`` for the editor's note (disabled too)."""
        from aqt.utils import tooltip

        from omnia.features.smart_notes.logic import rules_for_field

        note = getattr(editor, "note", None)
        if note is None:
            return
        rules = rules_for_field(
            list(self._settings().fields), _note_type_name(note), field
        )
        if not rules:
            tooltip("Omnia: no rule targets this field.")
            return
        self._generate_into_note(editor, note, rules)

    # --- review-time pre-generation --------------------------------------------------
    def _on_review_question(self, card: Any) -> None:
        if self._review is not None:
            self._review.on_card_shown(card)

    # --- shared helpers --------------------------------------------------------------
    def _settings(self) -> Any:
        return self._ctx.settings if self._ctx else None

    @staticmethod
    def _reload_editor(editor: Any) -> None:
        """Refresh the editor view across Anki versions (``loadNote`` / ``loadNoteKeepingFocus``)."""
        for attr in ("loadNoteKeepingFocus", "loadNote"):
            reload_view = getattr(editor, attr, None)
            if callable(reload_view):
                reload_view()
                return


def _sidebar_note_ids(sidebar_item: Any) -> Optional[list[int]]:
    """Return the note ids under a deck/note-type sidebar node, or None for other nodes.

    Anki's ``browser_sidebar_will_show_context_menu`` passes the clicked ``SidebarItem``; a
    deck node carries a ``full_name`` (search by ``deck:``) and a note-type node a ``name``
    (search by ``note:``). Any other node type isn't a batch target.
    """
    item_type = getattr(sidebar_item, "item_type", None)
    name = str(getattr(item_type, "name", "")).upper()
    if "NOTETYPE" in name:
        query = f'note:"{getattr(sidebar_item, "name", "")}"'
    elif "DECK" in name:
        query = f'deck:"{getattr(sidebar_item, "full_name", getattr(sidebar_item, "name", ""))}"'
    else:
        return None
    return anki_compat.find_card_note_ids(query)


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
