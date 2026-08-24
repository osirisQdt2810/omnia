"""The Smart Notes config dialog — a polished webview table over the note-type model.

A :class:`~omnia.gui.web_dialog.WebDialog`-hosted page (gradient header, rounded card, a
scrolling per-field table, light/dark) that edits ONE note type's
:class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` at a time: pick the note type,
designate its base (input) field + the decks it applies to, then for every other field set
whether to Generate it, whether to Lock it (blurs + protects from Auto-smart/Improve), its type
(text/image/sound), a prompt (edited in a popup, not inline), the ordered tool chain that
generates it (edited in the Tools picker; empty = the AI default), a kind-aware provider +
model, voice + language for sound fields, and an overwrite flag. Clicking the Generate / Lock /
Overwrite column header toggles that column for ALL rows. A ⚙ Options modal edits the global
flags (generate-at-review, regenerate-when-batching, allow-empty-sources).

This module is now a THIN shell: it builds the shared
:class:`~omnia.gui.smart_notes.dialogs.context.SmartNotesContext` + the responsibility-grouped
controllers (config table, prompt↔graph sync, authoring, account, native runtimes) and assembles
their ``ops()`` maps into the single ``pycmd`` handler dict. The pure row↔config mapping +
page markup live in ``html.py``; each controller is the thin Qt/Anki glue for its slice. Only
loaded inside Anki.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.theme import theme_manager

from omnia import active_voice_cache
from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers.catalog import catalog_payload
from omnia.core.runtime.native_runtime import default_manager
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.gui.smart_notes.dialogs.controllers import (
    AccountController,
    AuthoringController,
    ConfigController,
    GraphController,
    NativeRuntimeController,
    TransferController,
    UserToolsController,
)
from omnia.gui.smart_notes.html import build_smart_notes_html
from omnia.gui.web_dialog import WebDialog
from omnia.plugins.smart_notes.engine import LanguageDetector
from omnia.plugins.smart_notes.engine.tools import ToolContext, tools_catalog
from omnia.plugins.smart_notes.integration import SmartNotesStore

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository

logger = get_logger("smart_notes")


class SmartNotesDialog(WebDialog):
    """Per-note-type Smart Notes table: assembles the shared context + the controllers."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        # The shared context + the controllers are built BEFORE super().__init__ because the
        # handlers dict it needs is the union of the controllers' ops(). eval_js / parent_widget
        # are only available AFTER super().__init__ wires the webview, so the context resolves
        # them at call time via lambdas — safe because both are only ever touched from off-thread
        # success/failure callbacks (eval_js) or a user file-pick (parent_widget), long after
        # construction completes. Per-note-type rules persist in the collection (synced) via the
        # store; provider config (llm/tts) stays in the TOML config via repo; the native-runtime
        # sidecar manager (ADR-005) is built once so install state persists across its ops.
        self._ctx = SmartNotesContext(
            eval_js=lambda js: self.eval_js(js),
            parent_widget=lambda: self,
            repo=repo,
            store=SmartNotesStore(),
            native_manager=default_manager(),
            voice_cache=active_voice_cache(),
        )
        self._config = ConfigController(self._ctx, reject=self.reject)
        self._graph = GraphController(self._ctx)
        self._authoring = AuthoringController(self._ctx, self._graph)
        self._account = AccountController(self._ctx)
        self._native = NativeRuntimeController(self._ctx)
        self._user_tools = UserToolsController(self._ctx)
        self._transfer = TransferController(self._ctx)
        handlers = {
            **self._config.ops(),
            **self._graph.ops(),
            **self._authoring.ops(),
            **self._account.ops(),
            **self._native.ops(),
            **self._user_tools.ops(),
            **self._transfer.ops(),
        }
        super().__init__(
            parent,
            title="Smart Notes ✨",
            html=build_smart_notes_html(
                dark=theme_manager.night_mode,
                init=self._initial_state(),
                catalog=catalog_payload(self._ctx.cached_fetched_voices()),
                tools=self._tools_payload(),
            ),
            handlers=handlers,
            width=1040,
            height=640,
        )

    def closeEvent(self, evt: Any) -> None:  # noqa: N802 (Qt override name)
        """Drop anything the session staged, then let the base tear the webview down.

        A Try-it run may have copied a file the user picked into a temp folder so the test
        could resolve a media reference. It is session-scoped by design — nothing should
        survive the dialog that created it.
        """
        self._user_tools.dispose()
        super().closeEvent(evt)

    def _tools_payload(self) -> list[dict[str, Any]]:
        """The generation-tools catalog baked into the page for the per-row Tools picker.

        Baked (not fetched) for the same reason as the provider catalog: the first paint must
        not depend on a ``pycmd`` round-trip. Each tool reports what this machine is missing
        against the LIVE context, so the picker can print real advice beside it — which is why
        this needs the provider hub. With a broken provider config there is no hub to ask, and
        the picker degrades to its "no tools available" state (as every other provider-backed
        surface in this dialog does); ``build_hub`` has already logged why.

        The user's own tools are (re)loaded from ``user_files/tools`` first, so one authored in
        a previous session — or in THIS one, since the Tools tab reloads on save — is offered
        by the per-row picker like any builtin.
        """
        self._user_tools.ensure_loaded()
        hub = self._ctx.build_hub()
        if hub is None:
            return []
        return tools_catalog(
            ToolContext(
                providers=hub,
                # Catalog-only context: nothing here synthesizes speech, so the detector is off
                # (its LLM round-trip would be pure cost on the dialog's open path).
                detector=LanguageDetector(enabled=False),
                logger=logger,
            )
        )

    def _initial_state(self) -> dict[str, Any]:
        """The data baked into the page so it renders populated without an init pycmd.

        Seeds the note-type list + the first note type's load payload (base field, fields,
        rows, providers, options, graph, decks). The init pycmd callback is unreliable (the
        bridge channel isn't ready when the page's inline script first runs), so the first paint
        must not depend on it.
        """
        note_types = anki_compat.note_type_names()
        if not note_types:
            return {"note_types": []}
        return {
            "note_types": note_types,
            **self._config.load_payload_for(note_types[0]),
        }
