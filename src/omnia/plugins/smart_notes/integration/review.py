"""Review-time pre-generation for smart_notes (best-effort, current-card only).

The reference add-on keeps a generation buffer *ahead* of the reviewer by scanning the
scheduler's lookahead queue (``Scheduler.get_queued_cards``). That API is version-fragile on
the Anki 25.09 line, and a crash here would break the reviewer — which the task explicitly
forbids. So this implements the documented SIMPLER variant: when a card is shown, if it has
enabled smart-field rules whose target fields are still empty, generate just *that* card's
fields in the background and redraw it when done. No lookahead, no scheduler poking.

Gated by ``SmartNotesSettings.generate_at_review`` (default False). Every step is wrapped so a
failure logs and is swallowed rather than propagating into Anki's reviewer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine import (
    GenerationResult,
    GenerationService,
    applies_to_deck,
)
from omnia.plugins.smart_notes.integration.batch import materialize

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule, SmartNotesSettings


class ReviewTimeEvaluator:
    """Generates the shown card's empty smart fields in the background, then redraws it."""

    def __init__(
        self,
        service: GenerationService,
        settings_provider: Callable[[], Optional[SmartNotesSettings]],
    ) -> None:
        self._service = service
        # Settings are read FRESH each card (via the provider) so edits made mid-review apply.
        self._settings_provider = settings_provider
        self._log = get_logger("smart_notes")
        # Note ids currently being generated, so a re-show of the same card mid-flight doesn't
        # kick off a duplicate background wave.
        self._in_flight: set[int] = set()

    def on_card_shown(self, card: Any) -> None:
        """Hook callback for ``reviewer_did_show_question``: maybe pre-generate ``card``.

        Defensive by contract — any failure is logged and swallowed so the reviewer never
        crashes because of smart_notes. A no-op unless ``generate_at_review`` is enabled.
        """
        settings = self._settings_provider()
        if settings is None or not settings.generate_at_review or card is None:
            return
        try:
            self._maybe_generate(card, settings)
        except Exception:  # the reviewer must never crash because of this feature
            self._log.exception("smart_notes: review-time generation failed")

    def _maybe_generate(self, card: Any, settings: SmartNotesSettings) -> None:
        nid = int(card.nid)
        if nid in self._in_flight:
            return
        note = card.note()
        fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        config = settings.note_type_config(_note_type_name(note))
        if config is None or not applies_to_deck(config, int(card.did)):
            return
        # Only act when a generatable field's target is still empty — else nothing to fill.
        empty_targets = [
            f
            for f in config.generatable_fields()
            if not str(fields.get(f.field, "")).strip()
        ]
        if not empty_targets:
            return

        self._in_flight.add(nid)
        service = self._service

        def op() -> list[tuple[SmartNotesFieldRule, GenerationResult]]:
            return service.generate_note(
                config,
                fields,
                allow_empty_fields=settings.allow_empty_fields,
            )

        anki_compat.run_in_background(
            op,
            on_success=lambda results: self._apply(nid, results),
            on_failure=lambda exc: self._on_failure(nid, exc),
        )

    def _apply(
        self, nid: int, results: list[tuple[SmartNotesFieldRule, GenerationResult]]
    ) -> None:
        self._in_flight.discard(nid)
        if not results:
            return
        try:
            note = anki_compat.get_note(nid)
            wrote = False
            for rule, result in results:
                if rule.target_field not in note:
                    continue
                note[rule.target_field] = materialize(nid, rule, result)
                wrote = True
            if wrote:
                anki_compat.update_note(note)
                self._redraw_if_current(nid)
        except Exception:
            self._log.exception("smart_notes: failed to write review-time note %s", nid)

    def _on_failure(self, nid: int, exc: Exception) -> None:
        self._in_flight.discard(nid)
        self._log.exception("smart_notes: review-time generation error: %s", exc)

    @staticmethod
    def _redraw_if_current(nid: int) -> None:
        """Redraw the reviewer only if the just-filled note is still the visible card."""
        current = anki_compat.current_card()
        if current is not None and int(getattr(current, "nid", 0)) == nid:
            anki_compat.redraw_reviewer_current_card()


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
