"""Smart Notes feature: generate note fields (text/image) and audio via an AI provider.

Config is PER NOTE TYPE: each note type designates one BASE (input) field that is never
generated, and every other field has a per-field generation config (type + prompt template
referencing the base and other fields). Entry points share one generation core — a Browser
context-menu action, a deck/note-type sidebar batch, an editor ✨ button, a per-field
right-click menu, and optional review-time pre-generation — all running through
:meth:`GenerationService.generate_note` so chained fields, skip rules, and Markdown conversion
apply uniformly. Network runs off the Qt main thread via the threading seam; results are
written back to notes + media on the main thread. The pure logic lives in the ``engine`` /
``authoring`` subpackages; this module + the ``integration`` subpackage are the Anki glue.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.smart_notes.config import SmartNotesSettings
from omnia.plugins.smart_notes.engine import (
    GenerationResult,
    GenerationService,
)
from omnia.plugins.smart_notes.integration import (
    BatchGenerator,
    BatchSummary,
    IntegrationGateway,
    ReviewTimeEvaluator,
    SmartNotesStore,
    add_generate_button,
    materialize,
    set_button_enabled,
)

logger = get_logger("smart_notes")

_BROWSER_HOOK = "browser_will_show_context_menu"
_SIDEBAR_HOOK = "browser_sidebar_will_show_context_menu"
_EDITOR_HOOK = "editor_did_init_buttons"
_EDITOR_MENU_HOOK = "editor_will_show_context_menu"
_REVIEW_HOOK = "reviewer_did_show_question"
# Backend (anki.hooks) hook — fires on EVERY col.add_note, incl. AnkiConnect's addNote — that
# lets the integration gateway auto-generate externally-clipped cards (Feature B).
_NOTE_ADD_HOOK = "note_will_be_added"

# Omnia-branded label + hover tip shared by every "generate" entry point (browser / sidebar
# context menus) so the action is unmistakably this add-on's, not another's generic "generate".
_GEN_LABEL = "✨ Omnia · Generate Smart Fields"
_GEN_TIP = "[Omnia] Generate AI fields (text · image · audio) for the selected notes"


def _branded_gen_action(menu: Any, label: str = _GEN_LABEL, tip: str = _GEN_TIP) -> Any:
    """A QAction carrying the Omnia label + a hover tooltip, with menu tooltips made visible."""
    from aqt.qt import QAction

    action = QAction(label, menu)
    action.setToolTip(tip)
    action.setStatusTip(tip)
    menu.setToolTipsVisible(True)  # QMenu hides action tooltips unless this is on
    return action


@register("smart_notes")
class SmartNotesPlugin(FeaturePlugin):
    """AI generation of note fields + audio, driven from the Browser, sidebar, and editor."""

    name = "Smart Notes"
    description = (
        "Generate note fields (text/image) and audio (TTS) with an AI provider."
    )
    group = "AI"
    order = 50
    config_model = SmartNotesSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._service: Optional[GenerationService] = None
        self._review: Optional[ReviewTimeEvaluator] = None
        self._gateway: Optional[IntegrationGateway] = None
        self._store: Optional[SmartNotesStore] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._service = GenerationService(ctx.providers)
        # Rules persist in the collection (synced), read fresh each card via self._settings.
        self._store = SmartNotesStore()
        self._review = ReviewTimeEvaluator(self._service, self._settings)
        self._gateway = IntegrationGateway(self._service, self._settings)
        anki_compat.subscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.subscribe_hook(_SIDEBAR_HOOK, self._on_sidebar_menu)
        anki_compat.subscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)
        anki_compat.subscribe_hook(_EDITOR_MENU_HOOK, self._on_editor_context_menu)
        anki_compat.subscribe_hook(_REVIEW_HOOK, self._on_review_question)
        anki_compat.subscribe_anki_hook(
            _NOTE_ADD_HOOK, self._gateway.on_note_will_be_added
        )

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        anki_compat.unsubscribe_hook(_SIDEBAR_HOOK, self._on_sidebar_menu)
        anki_compat.unsubscribe_hook(_EDITOR_HOOK, self._on_editor_buttons)
        anki_compat.unsubscribe_hook(_EDITOR_MENU_HOOK, self._on_editor_context_menu)
        anki_compat.unsubscribe_hook(_REVIEW_HOOK, self._on_review_question)
        if self._gateway is not None:
            anki_compat.unsubscribe_anki_hook(
                _NOTE_ADD_HOOK, self._gateway.on_note_will_be_added
            )
        self._ctx = None
        self._service = None
        self._review = None
        self._gateway = None
        self._store = None

    # --- bespoke settings dialog -----------------------------------------------------
    def custom_config_dialog(self, repo: Any, parent: Any) -> Optional[Any]:
        # The per-note-type field table (base field + per-field generation config + ✨ auto-smart).
        from omnia.gui.smart_notes.dialogs import SmartNotesDialog

        return SmartNotesDialog(repo, parent)

    # --- Browser selection batch -----------------------------------------------------
    def _on_browser_menu(self, browser: Any, menu: Any) -> None:
        action = _branded_gen_action(menu)
        action.triggered.connect(lambda: self._generate_for_browser(browser))
        menu.addSeparator()
        menu.addAction(action)

    def _generate_for_browser(self, browser: Any) -> None:
        from aqt.utils import tooltip

        if not self._settings().note_types:
            tooltip(
                "Omnia: configure smart_notes for a note type first (Tools → Omnia)."
            )
            return
        self._run_batch(list(browser.selectedNotes()))

    # --- deck / note-type sidebar batch ----------------------------------------------
    def _on_sidebar_menu(
        self, _tree_view: Any, menu: Any, sidebar_item: Any, *_a: Any
    ) -> None:
        # Anki fires this hook as (tree_view, menu, sidebar_item, index); accept the trailing
        # args defensively so a signature tweak across versions can't break the menu.
        note_ids = _sidebar_note_ids(sidebar_item)
        if note_ids is None:
            return  # not a deck/note-type node
        action = _branded_gen_action(menu)
        action.triggered.connect(lambda: self._run_batch(note_ids))
        menu.addSeparator()
        menu.addAction(action)

    def _run_batch(self, note_ids: list[int]) -> None:
        from aqt.utils import tooltip

        service = self._service
        settings = self._settings()
        if service is None or not settings.note_types or not note_ids:
            tooltip("Omnia: no smart_notes config for the selection.")
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
        config = self._config_for_note(note)
        if note is None or config is None or not config.generatable_fields():
            tooltip("Omnia: open a note with smart_notes configured (Tools → Omnia).")
            return
        set_button_enabled(editor, False)
        self._generate_into_note(editor, note, config)

    def _generate_into_note(self, editor: Any, note: Any, config: Any) -> None:
        """Generate ``config``'s fields for ``note`` off-thread, then write + reload the editor."""
        service = self._service
        assert service is not None
        settings = self._settings()
        fields = {name: note[name] for name in note.keys()}  # noqa: SIM118

        def op() -> list[tuple[Any, GenerationResult]]:
            # Blocked fields stay empty (their hard prerequisites are missing); the editor only
            # writes generated results, so the block list is unused on this manual path.
            results, _blocked, _failed = service.generate_note(
                config,
                fields,
                allow_empty_fields=settings.allow_empty_fields,
            )
            return results

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
                logger.exception(
                    "smart_notes: failed to write field %s", rule.target_field
                )
        if written:
            # A brand-new note in the Add window has id==0 (not yet in the collection), and
            # col.update_note would raise NotFoundError on it. Persist only a saved note; the
            # editor reload renders the freshly generated fields on the in-memory note either way.
            if int(getattr(note, "id", 0)):
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
        from omnia.plugins.smart_notes.integration.field_menu import build_field_menu

        editor = getattr(editor_webview, "editor", editor_webview)
        build_field_menu(self._ctx, editor, menu, self._generate_field)

    def _generate_field(self, editor: Any, field: str) -> None:
        """Generate just ``field`` for the editor's note, on demand (even if disabled)."""
        from aqt.utils import tooltip

        from omnia.plugins.smart_notes.integration.field_menu import (
            single_field_config,
        )

        note = getattr(editor, "note", None)
        config = self._config_for_note(note)
        one = single_field_config(config, field) if config else None
        if one is None:
            tooltip("Omnia: no smart_notes config targets this field.")
            return
        self._generate_into_note(editor, note, one)

    # --- review-time pre-generation --------------------------------------------------
    def _on_review_question(self, card: Any) -> None:
        if self._review is not None:
            self._review.on_card_shown(card)

    # --- shared helpers --------------------------------------------------------------
    def _settings(self) -> Any:
        # Per-note-type rules live in the collection (synced); read fresh on each access.
        return self._store.load() if self._store else None

    def _config_for_note(self, note: Any) -> Any:
        """Return the note's note-type smart-notes config, or None."""
        settings = self._settings()
        if note is None or settings is None:
            return None
        return settings.note_type_config(_note_type_name(note))

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
        term = anki_compat.escape_search_term(str(getattr(sidebar_item, "name", "")))
        query = f'note:"{term}"'
    elif "DECK" in name:
        deck_name = getattr(
            sidebar_item, "full_name", getattr(sidebar_item, "name", "")
        )
        query = f'deck:"{anki_compat.escape_search_term(str(deck_name))}"'
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
