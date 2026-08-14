"""Which maintenance settings a note gets: the per-note-type map, resolved and planned.

Every interesting task option names a FIELD (``source_field``, ``target_field``, ``field``, the
``{source: target}`` maps), and field names are a property of a note type — "Synonyms" exists on
one and not on the next. So one global set of task options could never fit a collection: it
silently did nothing on every note type it was not written for. Settings are therefore stored
per note type, several note types can be switched on at once, and a run applies each note's OWN
note type's settings.

Storage (a NEW key beside the old one — nothing is rewritten in place)::

    [note_maintenance.note_types."Vocabulary"]
    enable = true

    [note_maintenance.note_types."Vocabulary".tasks.strip_ipa]
    enable = true
    fields = { Synonyms = "SynonymsNoIPA" }

**What an OLDER Omnia does with this blob** (ADR-010, and the reason the key is additive):
:class:`~omnia.plugins.note_maintenance.config.NoteMaintenanceSettings` is a ``PersistedModel``,
so a build that has never heard of ``note_types`` loads it as an extra key, round-trips it
through ``.dict()``, and hands it back to storage untouched — it does not crash, and its
settings panel (which merges onto the raw stored section and writes only ``tasks``) does not
delete it either. That older build keeps running the legacy global ``[note_maintenance.tasks]``
map, which THIS version never rewrites and never runs. The two coexist: each device runs the
map it understands, and neither destroys the other's.

Skipping is reported, never silent: a note whose type has no settings is counted into the
plan's :attr:`~omnia.plugins.note_maintenance.runner.ChangePlan.skipped`, and the preview says
so. "Nothing happened" and "nothing happened because this note type is not set up" must not
look the same.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional, Union

from omnia.core.logging import get_logger
from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.registry import build_tasks
from omnia.plugins.note_maintenance.runner import (
    ChangePlan,
    MaintenanceRunner,
    NoteChange,
    SkippedNotes,
    SkipReason,
)

logger = get_logger("note_maintenance")


def _key(note_type: str) -> str:
    """The lookup key for a note type name (surrounding space and case do not decide identity)."""
    return note_type.strip().lower()


class NoteTypeScope:
    """The stored ``note_types`` map, answered one note type at a time.

    Reads the map RAW and never rewrites it: an entry may be anything at all (a shape a newer
    Omnia introduced, or a hand-edited ``features.toml``), and every question below has an
    answer for that case — "not configured" — instead of an exception in a Qt slot.
    """

    def __init__(self, stored: Mapping[str, Any]) -> None:
        """Index ``stored``.

        Args:
            stored: The ``[note_maintenance.note_types]`` map as stored,
                ``{note type name: {"enable": bool, "tasks": {…}}}``.
        """
        self._entries: dict[str, Any] = dict(stored)
        self._index: dict[str, str] = {}
        for name in self._entries:
            # First spelling wins, so a duplicate that differs only in case cannot silently
            # take over the settings the panel wrote.
            self._index.setdefault(_key(name), name)

    @property
    def names(self) -> tuple[str, ...]:
        """Every configured note type name, in stored order and stored spelling."""
        return tuple(self._entries)

    def stored_name(self, note_type: str) -> Optional[str]:
        """Return the stored spelling of ``note_type``, or None when it has no entry at all."""
        return self._index.get(_key(note_type))

    def entry(self, note_type: str) -> dict[str, Any]:
        """Return ``note_type``'s stored settings table (``{}`` when absent or not a table)."""
        name = self.stored_name(note_type)
        value = self._entries.get(name) if name is not None else None
        return dict(value) if isinstance(value, Mapping) else {}

    def is_configured(self, note_type: str) -> bool:
        """Whether this note type has settings a run can read."""
        name = self.stored_name(note_type)
        return name is not None and isinstance(self._entries[name], Mapping)

    def is_enabled(self, note_type: str) -> bool:
        """Whether ``note_type`` takes part in a run.

        A stored entry with no ``enable`` key counts as ON: it exists because someone put
        settings there, and the panel always writes the tick explicitly.
        """
        return self.is_configured(note_type) and bool(
            self.entry(note_type).get("enable", True)
        )

    def task_sections(self, note_type: str) -> dict[str, Any]:
        """Return ``note_type``'s RAW ``{task id: options}`` map (``{}`` when it has none)."""
        tasks = self.entry(note_type).get("tasks")
        return dict(tasks) if isinstance(tasks, Mapping) else {}


class NoteTypePlanner:
    """Plans ONE run over notes of several note types, each with its own task settings.

    The Browser hands over whatever the user selected, which routinely spans note types. Each
    note is planned by the runner built from ITS note type's settings, and a note type that
    contributes nothing is counted with the reason why — the plan carries both halves.

    Runners are built once per note type and reused, so a 5 000-note selection parses each
    note type's config once, not once per note.
    """

    def __init__(self, scope: NoteTypeScope) -> None:
        """Initialise the planner.

        Args:
            scope: The per-note-type settings to resolve against.
        """
        self._scope = scope
        self._runs: dict[str, Union[MaintenanceRunner, SkipReason]] = {}

    @property
    def has_runnable_note_type(self) -> bool:
        """Whether ANY configured note type would actually run a task.

        Lets the Browser entry point say "nothing is set up yet" without reading a single note
        (the resolution is cached, so the scan that follows pays nothing for this).
        """
        return any(
            isinstance(self._run_for(name), MaintenanceRunner)
            for name in self._scope.names
        )

    def plan(self, notes: Iterable[NoteView]) -> ChangePlan:
        """Return what ``notes`` would change, plus the ones no settings covered.

        Args:
            notes: The selected note snapshots, in any mix of note types.

        Returns:
            A :class:`~omnia.plugins.note_maintenance.runner.ChangePlan` holding the changes in
            the order the notes came in, and one
            :class:`~omnia.plugins.note_maintenance.runner.SkippedNotes` entry per (note type,
            reason) that was passed over.
        """
        changes: list[NoteChange] = []
        skipped: dict[tuple[str, SkipReason], int] = {}
        for note in notes:
            run = self._run_for(note.note_type)
            if isinstance(run, SkipReason):
                key = (note.note_type, run)
                skipped[key] = skipped.get(key, 0) + 1
                continue
            change = run.plan_note(note)
            if change is not None:
                changes.append(change)
        return ChangePlan(
            tuple(changes),
            tuple(
                SkippedNotes(note_type=note_type, reason=reason, note_count=count)
                # dict order = the order the note types were first met in the selection.
                for (note_type, reason), count in skipped.items()
            ),
        )

    def _run_for(self, note_type: str) -> Union[MaintenanceRunner, SkipReason]:
        """The runner for ``note_type``, or why it has none (built once, then cached)."""
        if note_type not in self._runs:
            self._runs[note_type] = self._build(note_type)
        return self._runs[note_type]

    def _build(self, note_type: str) -> Union[MaintenanceRunner, SkipReason]:
        """Resolve ``note_type``'s settings into a runner, or the reason there is none."""
        if not self._scope.is_configured(note_type):
            if self._scope.stored_name(note_type) is not None:
                # An entry exists but is not a table. It stays exactly as stored (the settings
                # panel merges onto the raw map); it just cannot be read as settings.
                logger.error(
                    "note_maintenance: note type %r settings are not a table; skipping it",
                    note_type,
                )
            return SkipReason.UNCONFIGURED
        if not self._scope.is_enabled(note_type):
            return SkipReason.DISABLED
        runner = MaintenanceRunner(build_tasks(self._scope.task_sections(note_type)))
        return runner if runner.active_tasks else SkipReason.NO_TASKS
