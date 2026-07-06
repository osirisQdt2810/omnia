"""External-integration gateway: auto-generate smart fields for pushed notes (Feature B).

When a third-party source (the Omnia browser extension, via AnkiConnect's ``addNote``) adds a
note through Anki's backend, the ``note_will_be_added`` hook fires. This gateway inspects the
freshly-added note and, only when BOTH guards pass, schedules a one-shot background generation
of the note's empty smart fields — reusing the same :class:`BatchGenerator` the Browser/sidebar
batches use:

* caller guard: the clipper tags the note ``omnia-autogen`` (opt-in on its side); and
* Omnia guard: the per-integration toggle in Smart Notes options (default OFF — no surprise
  LLM spend).

The write-back path uses ``update_note`` (which does NOT fire ``note_will_be_added``), so there
is no loop. Every hook callback is fully defensive: a failure logs and is swallowed, never
propagating into Anki's note-add path. ``aqt`` is imported lazily inside methods so the module
unit-tests headless.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine import GenerationService, applies_to_deck
from omnia.plugins.smart_notes.integration.batch import BatchGenerator, BatchSummary
from omnia.plugins.smart_notes.integration.integrations import (
    AUTOGEN_TAG,
    integration_for_tags,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import SmartNotesSettings

logger = get_logger("smart_notes")


class IntegrationGateway:
    """Auto-generates smart fields for externally-pushed notes, gated by two guards."""

    # Collect note ids added within this window into ONE batch. Rapid clipping (many "+" clicks)
    # must not spawn one background generation + modal progress dialog per note — that stacks
    # dialogs and freezes Anki. A single trailing timer flushes the whole burst as one batch.
    _DEBOUNCE_MS = 900

    def __init__(
        self,
        service: GenerationService,
        settings_provider: Callable[[], Optional[SmartNotesSettings]],
    ) -> None:
        self._service = service
        # Settings are read FRESH each add (via the provider) so a toggle change applies at once.
        self._settings_provider = settings_provider
        self._pending: set[int] = set()  # note ids queued for the next batch
        self._running = False  # a batch is in flight (serialize; never overlap)
        self._timer_armed = False  # a debounce flush is scheduled

    def on_note_will_be_added(self, col: Any, note: Any, deck_id: Any) -> None:
        """``note_will_be_added`` callback: queue the note for a coalesced batch when guards pass.

        Defensive by contract — any failure is logged and swallowed so a note add never crashes
        because of smart_notes. Cheap-gates on the caller tag first so ordinary adds pay almost
        nothing.

        Args:
            col: The collection the note is being added to (unused; the deferred work reads
                ``mw.col`` after the commit).
            note: The note about to be added (its ``id`` is not assigned yet — hence the defer).
            deck_id: The id of the deck the note will land in.
        """
        try:
            self._maybe_enqueue(note, deck_id)
        except Exception:  # a note add must never crash because of this feature
            logger.exception("smart_notes: integration gateway failed")

    def _maybe_enqueue(self, note: Any, deck_id: Any) -> None:
        # Cheap guard first: no caller tag → not our concern (the vast majority of adds).
        if not _has_tag(note, AUTOGEN_TAG):
            return
        integration = integration_for_tags(_note_tags(note))
        if integration is None:
            return
        settings = self._settings_provider()
        if settings is None or not settings.integration_autogen_enabled(
            integration.key
        ):
            return
        config = settings.note_type_config(_note_type_name(note))
        if config is None or not applies_to_deck(config, int(deck_id)):
            return
        # Only act when a generatable field's target is still empty — else nothing to fill.
        fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        has_empty = any(
            not str(fields.get(f.field, "")).strip()
            for f in config.generatable_fields()
        )
        if not has_empty:
            return
        # Defer past the current add_note commit so note.id is assigned before we read it.
        # note_will_be_added fires from add_note BEFORE `note.id` is set, and the add runs ON THE
        # MAIN THREAD (AnkiConnect's QTimer, Anki's Add dialog). taskman.run_on_main would execute
        # its closure SYNCHRONOUSLY there (Qt direct signal) — i.e. still inside add_note, with
        # note.id == 0. QTimer.singleShot(0) always posts to the event loop, so the closure runs on
        # the NEXT tick, after add_note returns and the note id is committed.
        from aqt.qt import QTimer

        QTimer.singleShot(0, lambda: self._enqueue(note))

    def _enqueue(self, note: Any) -> None:
        """Add the just-committed note's id to the pending batch + arm the debounce (main thread)."""
        try:
            nid = int(getattr(note, "id", 0) or 0)
            if not nid:
                return
            self._pending.add(nid)
            self._arm_flush()
        except Exception:
            logger.exception("smart_notes: integration enqueue failed")

    def _arm_flush(self) -> None:
        """Schedule one flush ``_DEBOUNCE_MS`` after the FIRST pending add (coalesces a burst)."""
        if self._timer_armed:
            return
        self._timer_armed = True
        from aqt.qt import QTimer

        QTimer.singleShot(self._DEBOUNCE_MS, self._flush)

    def _flush(self) -> None:
        """Run the queued note ids as ONE background batch (serialized — never overlapping)."""
        self._timer_armed = False
        try:
            if self._running:
                # A batch is still generating — retry after another window so we never stack runs.
                self._arm_flush()
                return
            if not self._pending:
                return
            nids = sorted(self._pending)
            self._pending.clear()
            self._run_batch(nids)
        except Exception:
            logger.exception("smart_notes: integration flush failed")

    def _run_batch(self, nids: list[int]) -> None:
        settings = self._settings_provider()
        if settings is None:
            return
        # Drop the one-shot ``omnia-autogen`` tag before generating (keeps the source tag). This
        # uses update_note, which does NOT fire note_will_be_added, so it can't re-trigger us.
        self._clear_autogen_tags(nids)
        self._running = True

        def done(summary: BatchSummary) -> None:
            self._running = False
            _on_done(summary)
            # Notes queued while this batch ran get their own flush.
            if self._pending:
                self._arm_flush()

        # show_progress=False: background auto-gen must not open a modal dialog (that is what
        # froze Anki when many clips ran back-to-back); a summary tooltip still reports the result.
        try:
            BatchGenerator(self._service, settings).run(nids, done, show_progress=False)
        except Exception:
            # run() does synchronous work (build plans / read notes) before dispatching; if it
            # raises we must clear _running here — otherwise the async ``done`` never fires and the
            # gateway would wedge (every future flush sees _running and only re-arms).
            self._running = False
            logger.exception("smart_notes: integration batch dispatch failed")

    @staticmethod
    def _clear_autogen_tags(nids: list[int]) -> None:
        """Remove the one-shot ``omnia-autogen`` tag from each note id and persist it."""
        import aqt

        col = aqt.mw.col
        for nid in nids:
            try:
                fresh = col.get_note(nid)
                if fresh.has_tag(AUTOGEN_TAG):
                    fresh.remove_tag(AUTOGEN_TAG)
                    col.update_note(fresh)
            except Exception:
                logger.exception("smart_notes: failed clearing autogen tag for %s", nid)


def _on_done(summary: BatchSummary) -> None:
    """Report the one-shot generation outcome as a tooltip (matches the batch summary)."""
    from aqt.utils import tooltip

    tooltip(f"Omnia: {summary.message()}")


def _has_tag(note: Any, tag: str) -> bool:
    """True if ``note`` carries ``tag`` (``has_tag`` when available, else the tags list)."""
    has_tag = getattr(note, "has_tag", None)
    if callable(has_tag):
        return bool(has_tag(tag))
    return tag in _note_tags(note)


def _note_tags(note: Any) -> list[str]:
    """Return the note's tags as a list (Anki exposes ``note.tags``)."""
    return list(getattr(note, "tags", []) or [])


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
