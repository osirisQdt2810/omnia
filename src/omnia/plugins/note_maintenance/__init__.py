"""Note Maintenance feature: repair and reshape the text a collection ALREADY holds.

Smart Notes *generates* content with an LLM; this plugin *repairs* content, deterministically
and for free — no tokens, no network, no provider configured. The two are complementary: a
user without an API key still gets value, and a batch of 5 000 notes can be reformatted
without a bill.

The feature is one plugin hosting many small *tasks* (strip IPA, extract an audio file name,
re-pair synonyms, …). Tasks are registered in a plugin-local registry rather than promoted to
plugins of their own: they are numerous, homogeneous, and share one runner, one preview and
one apply path.

The moving parts, split so everything but the last one is unit-testable headless:

* :mod:`~omnia.plugins.note_maintenance.base` — the task contract (``NoteView`` in, changed
  fields out);
* :mod:`~omnia.plugins.note_maintenance.registry` — ``@register_task`` + config → tasks;
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
from omnia.plugins.note_maintenance.registry import build_tasks
from omnia.plugins.note_maintenance.runner import ChangePlan, MaintenanceRunner

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
        runner = self.build_runner()
        if not runner.active_tasks:
            tooltip(
                "Omnia: no maintenance task is switched on (configure Note Maintenance)."
            )
            return
        anki_compat.run_in_background(
            lambda: runner.plan(_note_views(note_ids)),
            on_success=lambda plan: self._preview(plan, browser),
            parent=browser,
            label=f"Omnia: checking {len(note_ids)} note(s)…",
        )

    def _preview(self, plan: ChangePlan, parent: Any) -> None:
        """Open the diff preview for ``plan`` (the only route to a write)."""
        from aqt.utils import tooltip

        if plan.is_empty:
            tooltip("Omnia: the selected notes need no maintenance.")
            return
        from omnia.gui.note_maintenance.preview_dialog import MaintenancePreviewDialog

        MaintenancePreviewDialog(plan, parent).exec()

    def build_runner(self) -> MaintenanceRunner:
        """Return a runner holding this plugin's configured tasks.

        Reads the ``tasks`` namespace from the plugin's settings (defaults when the plugin is
        not enabled), so the caller — the preview dialog — never parses config itself.

        Raises:
            pydantic.ValidationError: If a task's config section holds an invalid option.
        """
        return MaintenanceRunner(build_tasks(self._settings().tasks))

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
