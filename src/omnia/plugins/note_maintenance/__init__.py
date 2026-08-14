"""Note Maintenance feature: repair and reshape the text a collection ALREADY holds.

Smart Notes *generates* content with an LLM; this plugin *repairs* content, deterministically
and for free — no tokens, no network, no provider configured. The two are complementary: a
user without an API key still gets value, and a batch of 5 000 notes can be reformatted
without a bill.

The feature is one plugin hosting many small *tasks* (strip IPA, extract an audio file name,
re-pair synonyms, …). Tasks are registered in a plugin-local registry rather than promoted to
plugins of their own: they are numerous, homogeneous, and share one runner, one preview and
one apply path.

Task settings are **per note type** — a task option that names a field can only mean something
inside one note type — and several note types can be switched on and maintained in one pass;
see :mod:`~omnia.plugins.note_maintenance.note_types` for the stored shape and for what an
OLDER Omnia does with it.

The moving parts, split so everything but the last one is unit-testable headless:

* :mod:`~omnia.plugins.note_maintenance.base` — the task contract (``NoteView`` in, changed
  fields out);
* :mod:`~omnia.plugins.note_maintenance.registry` — ``@register_task`` + config → tasks;
* :mod:`~omnia.plugins.note_maintenance.note_types` — a note's type → the settings it gets;
* :mod:`~omnia.plugins.note_maintenance.runner` — notes + tasks → a ``ChangePlan`` (never writes);
* :mod:`~omnia.plugins.note_maintenance.diff` — the plan rendered as an inline HTML diff;
* :mod:`~omnia.plugins.note_maintenance.apply` — the ONLY writer, inside a ``CollectionOp``.

A run is always user-initiated and always previewed: nothing happens on enable, on a timer, or
without the user confirming the diff.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register

# Importing the task package runs every task's @register_task decorator (registration side
# effect only — the pyproject per-file-ignore for __init__.py allows the "unused" import).
from omnia.plugins.note_maintenance import tasks
from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings
from omnia.plugins.note_maintenance.note_types import NoteTypePlanner, NoteTypeScope
from omnia.plugins.note_maintenance.runner import ChangePlan

logger = get_logger("note_maintenance")

_BROWSER_HOOK = "browser_will_show_context_menu"
# Omnia-branded label + hover tip, so the entry is unmistakably this add-on's (mirrors the
# smart_notes Browser action).
_MENU_LABEL = "🧹 Omnia · Maintain Notes…"
_MENU_TIP = "[Omnia] Clean up the selected notes — you review every change before anything is written"


@register("note_maintenance")
class NoteMaintenancePlugin(FeaturePlugin):
    """Batch-cleans existing notes with deterministic, provider-free text tasks."""

    name = "Note Maintenance"
    description = "Clean up and reformat the text your notes already contain."
    group = "Editing"
    tooltip = (
        "Deterministic, free batch edits over notes you already have — no LLM, no network, "
        "no API key.\n"
        "\n"
        "• Tasks: strip IPA out of a synonym list, pull the file name out of a [sound:…] "
        "reference, re-pair synonyms with their transcriptions, refill an example sentence "
        "from its clozed twin, find-and-replace a literal string across every field.\n"
        "• Each task has its own on/off switch and a run order, so tasks touching the same "
        "field layer predictably instead of fighting.\n"
        "• Settings are per NOTE TYPE, with every field picked from a dropdown of that note "
        "type's real fields — tick as many note types as you like and maintain them in one "
        "pass; a note whose type you have not set up is reported, never guessed at.\n"
        "• Nothing is written until you confirm the diff, and the whole batch is a single "
        "undo step (Ctrl+Z puts it all back)."
    )
    order = 60
    config_model = NoteMaintenanceSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None

    def on_enable(self, ctx: PluginContext) -> None:
        # Only the Browser entry point is installed: enabling the feature must never edit a
        # note by itself. The user picks the notes, asks for the run, and confirms the diff.
        self._ctx = ctx
        anki_compat.subscribe_hook(_BROWSER_HOOK, self._on_browser_menu)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook(_BROWSER_HOOK, self._on_browser_menu)
        self._ctx = None

    # --- bespoke settings dialog -----------------------------------------------------
    def custom_config_dialog(self, repo: Any, parent: Any) -> Optional[Any]:
        """The per-task panel (the flat form cannot express a task's field-mapping options)."""
        from omnia.gui.note_maintenance.panel import NoteMaintenanceSettingsDialog

        return NoteMaintenanceSettingsDialog(repo, parent)

    # --- Browser entry point ---------------------------------------------------------
    def _on_browser_menu(self, browser: Any, menu: Any) -> None:
        """Add the maintenance entry to the Browser's note context menu."""
        from aqt.qt import QAction

        action = QAction(_MENU_LABEL, menu)
        action.setToolTip(_MENU_TIP)
        action.setStatusTip(_MENU_TIP)
        menu.setToolTipsVisible(True)  # QMenu hides action tooltips unless this is on
        action.triggered.connect(lambda: self.maintain_notes(browser))
        menu.addSeparator()
        menu.addAction(action)

    def maintain_notes(self, browser: Any) -> None:
        """Plan a maintenance run over the Browser's selected notes and open the preview.

        The selection routinely spans note types, so each note is planned with ITS OWN note
        type's settings and a note type that has none is reported rather than passed over in
        silence (see :class:`~omnia.plugins.note_maintenance.note_types.NoteTypePlanner`).

        The scan (reading every selected note and running the tasks over it) happens OFF the
        Qt main thread — a 5 000-note selection would otherwise freeze the Browser — and only
        the preview dialog is opened back on it. Nothing is written here: the plan is a
        proposal until the user confirms it.

        Args:
            browser: Anki's Browser window (the selection source and the dialog's parent).
        """
        from aqt.utils import tooltip

        note_ids = [int(nid) for nid in browser.selectedNotes()]
        if not note_ids:
            tooltip("Omnia: select the notes to maintain first.")
            return
        planner = self.build_planner()
        if not planner.has_runnable_note_type:
            tooltip(self._nothing_configured_message())
            return
        anki_compat.run_in_background(
            lambda: planner.plan(_note_views(note_ids)),
            on_success=lambda plan: self._preview(plan, browser),
            parent=browser,
            label=f"Omnia: checking {len(note_ids)} note(s)…",
        )

    def _preview(self, plan: ChangePlan, parent: Any) -> None:
        """Open the diff preview for ``plan`` (the only route to a write)."""
        from aqt.utils import tooltip

        if plan.is_empty:
            # A selection whose note types have no settings changes nothing — and looks exactly
            # like a selection that needed no maintenance. Say which it was.
            summary = plan.skip_summary
            tooltip(
                f"Omnia: nothing to maintain. {summary}"
                if summary
                else "Omnia: the selected notes need no maintenance."
            )
            return
        from omnia.gui.note_maintenance.preview_dialog import MaintenancePreviewDialog

        MaintenancePreviewDialog(plan, parent).exec()

    def _nothing_configured_message(self) -> str:
        """What to say when no note type is set up — which differs for an UPGRADING user.

        Task settings used to be one global map. A user who had that map working reads "no note
        type has a maintenance task switched on" as "my settings are gone", and it is the first
        thing they see after updating. Their settings are in fact untouched and are offered as
        the starting point for the first note type they configure, so this says so.
        """
        base = "Omnia: no note type has a maintenance task switched on"
        if self._settings().tasks:
            return (
                f"{base}. Your existing task settings are kept — open Note Maintenance and "
                "tick a note type to apply them to it."
            )
        return f"{base} (configure Note Maintenance)."

    def build_planner(self) -> NoteTypePlanner:
        """Return a planner over this plugin's per-note-type settings.

        Reads the ``note_types`` namespace from the plugin's settings (defaults when the plugin
        is not enabled), so the caller never parses config itself. Neither the note-type map nor
        a task section this version cannot parse raises here — an unreadable note type is
        skipped and reported, and an unreadable task falls back to that task's defaults inside
        :func:`~omnia.plugins.note_maintenance.registry.build_tasks` — because the only caller
        is the Qt slot behind a menu entry.
        """
        return NoteTypePlanner(NoteTypeScope(self._settings().note_types))

    def _settings(self) -> NoteMaintenanceSettings:
        """The plugin's settings, falling back to defaults when it is not enabled."""
        settings = getattr(self._ctx, "settings", None) if self._ctx else None
        return settings if settings is not None else NoteMaintenanceSettings()


def _note_views(note_ids: Sequence[int]) -> list[NoteView]:
    """Read ``note_ids`` out of the collection as the immutable snapshots tasks work on.

    Runs off the main thread inside the scan's background op. A note that cannot be read (it
    was deleted between the selection and the scan) is SKIPPED — one missing note must not cost
    the user the whole run.

    Args:
        note_ids: The selected note ids.

    Returns:
        One :class:`NoteView` per readable note, keeping the note type's own field order.
    """
    views: list[NoteView] = []
    for note_id in note_ids:
        note = anki_compat.get_note_or_none(int(note_id))
        if note is None:
            logger.warning("note %s is gone; skipping it", note_id)
            continue
        views.append(
            NoteView(
                note_id=int(note_id),
                note_type=_note_type_name(note),
                # items() preserves the NOTE TYPE's field order — the order the preview shows.
                fields={name: str(value) for name, value in note.items()},
            )
        )
    return views


def _note_type_name(note: Any) -> str:
    """The note's note-type name, or ``""`` when it can't be resolved."""
    model = note.note_type()
    return str((model or {}).get("name", ""))
