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

from typing import Optional

from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register

# Importing the task package runs every task's @register_task decorator (registration side
# effect only — the pyproject per-file-ignore for __init__.py allows the "unused" import).
from omnia.plugins.note_maintenance import tasks
from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings
from omnia.plugins.note_maintenance.registry import build_tasks
from omnia.plugins.note_maintenance.runner import MaintenanceRunner


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
        # Nothing to hook: a maintenance run is user-initiated (the Browser entry point lands
        # with the preview dialog). Enabling only makes the feature available.
        self._ctx = ctx

    def on_disable(self, ctx: PluginContext) -> None:
        self._ctx = None

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
