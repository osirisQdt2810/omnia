"""Word Lookup feature: answer "is this word already in my collection?" for the desktop clipper.

The companion desktop clipper floats a magnifier next to its "+" over whatever app you are
reading. Clicking it must show the matching card — but the clipper is a separate process, and
the decisions worth making centrally (which note types count, which of a 35-field note type is
worth showing, how hits are ranked) belong with the collection, not duplicated in every client.

So this plugin owns the *logic* and exposes ONE read-only loopback endpoint; the clipper stays a
thin renderer. The split is deliberate:

* :mod:`~omnia.plugins.word_lookup.logic` — pure ranking/triage/cleaning (unit-tested headless);
* :mod:`~omnia.plugins.word_lookup.service` — the loopback HTTP surface + main-thread marshalling;
* this module — the only part that touches Anki: reading notes/cards out of the collection.

Everything the endpoint returns is display-ready, so a client never has to understand Anki's
field HTML, cloze markup, ``[sound:…]`` refs, or scheduler internals.
"""

from __future__ import annotations

from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.word_lookup.config import WordLookupSettings
from omnia.plugins.word_lookup.logic import (
    LookupCard,
    LookupField,
    build_query,
    card_state,
    rank_cards,
    triage_fields,
)
from omnia.plugins.word_lookup.service import LookupService

logger = get_logger("word_lookup")


@register("word_lookup")
class WordLookupPlugin(FeaturePlugin):
    """Serves word lookups from the collection to the companion desktop clipper."""

    name = "Word Lookup"
    description = "Let the desktop clipper look a word up in your collection."
    group = "Integrations"
    tooltip = (
        "Answers the desktop clipper's magnifier: “is this word already in my collection?”\n"
        "\n"
        "• Runs a tiny read-only service on 127.0.0.1 (loopback only — never reachable from "
        "the network, and it cannot write to your collection).\n"
        "• Searchable note types: leave empty to search everything, or list the few you study.\n"
        "• A matching note is returned display-ready: the empty fields of a big note type are "
        "dropped, the rest keep the note type's own field order, and the card's state "
        "(new/learning/review + interval, reps, lapses) comes along.\n"
        "• Turn this off and the clipper's magnifier simply reports the lookup service is "
        "unavailable; nothing else changes."
    )
    order = 50
    config_model = WordLookupSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._service: Optional[LookupService] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._service = LookupService(
            self.lookup,
            port=int(getattr(ctx.settings, "port", 8766)),
            run_on_main=anki_compat.run_on_main,
        )
        if not self._service.start():
            logger.warning(
                "word_lookup: lookup service did not start (port %s in use?)",
                getattr(ctx.settings, "port", 8766),
            )

    def on_disable(self, ctx: PluginContext) -> None:
        if self._service is not None:
            self._service.stop()
        self._service = None
        self._ctx = None

    # -- the endpoint's payload ----------------------------------------------------------

    def lookup(self, word: str) -> dict[str, Any]:
        """Search the collection for ``word`` and return a display-ready payload.

        Runs on the Qt main thread (the service marshals it there) because it reads the
        collection. Never raises for an ordinary miss — a word that is simply not in the
        collection is a successful lookup with no cards.

        Args:
            word: The word/phrase the clipper captured.

        Returns:
            ``{"word", "found", "cards": [...], "truncated": bool}`` where each card carries its
            title, triaged fields, deck, tags and scheduling state.
        """
        settings = self._settings()
        query = build_query(word, tuple(settings.note_types))
        if not query:
            return {"word": word, "found": False, "cards": [], "truncated": False}
        note_ids = anki_compat.find_note_ids(query)
        limit = max(1, int(settings.max_results))
        cards: list[LookupCard] = []
        for nid in note_ids[: limit * 3]:  # over-read a little so ranking has room to reorder
            card = self._card_for_note(nid, settings)
            if card is not None:
                cards.append(card)
        ranked = rank_cards(cards, word)[:limit]
        return {
            "word": word,
            "found": bool(ranked),
            "truncated": len(note_ids) > limit,
            "cards": [self._card_payload(card) for card in ranked],
        }

    def _settings(self) -> WordLookupSettings:
        """The plugin's settings, falling back to defaults when it is not enabled."""
        settings = getattr(self._ctx, "settings", None) if self._ctx else None
        return settings if settings is not None else WordLookupSettings()

    def _card_for_note(
        self, nid: int, settings: WordLookupSettings
    ) -> Optional[LookupCard]:
        """Build one :class:`LookupCard` from a note id, or ``None`` if it can't be read.

        Best-effort per note: a note deleted between the search and this read, or an
        unexpected shape, is skipped rather than failing the whole lookup.
        """
        try:
            note = anki_compat.get_note(nid)
            note_type = self._note_type_name(note)
            # items() preserves the NOTE TYPE's field order, which the triage uses as its
            # relevance signal — never sort or re-key this.
            ordered = [(name, str(value)) for name, value in note.items()]
            title, fields = triage_fields(
                ordered,
                max_fields=int(settings.max_fields),
                hidden=tuple(settings.hidden_fields),
            )
            first_card = self._first_card(note)
            return LookupCard(
                note_id=int(nid),
                note_type=note_type,
                deck=self._deck_name(first_card),
                title=title or str(nid),
                fields=tuple(fields),
                tags=tuple(getattr(note, "tags", []) or []),
                state=card_state(getattr(first_card, "type", 2) or 0),
                interval_days=int(getattr(first_card, "ivl", 0) or 0),
                reps=int(getattr(first_card, "reps", 0) or 0),
                lapses=int(getattr(first_card, "lapses", 0) or 0),
            )
        except Exception:
            logger.exception("word_lookup: could not read note %s", nid)
            return None

    @staticmethod
    def _note_type_name(note: Any) -> str:
        """The note's note-type name (best-effort across Anki versions)."""
        try:
            model = note.note_type()
        except Exception:
            return ""
        return str((model or {}).get("name", ""))

    @staticmethod
    def _first_card(note: Any) -> Any:
        """The note's first card, or ``None`` (a note always has one in practice)."""
        try:
            cards = note.cards()
        except Exception:
            return None
        return cards[0] if cards else None

    @staticmethod
    def _deck_name(card: Any) -> str:
        """The card's deck name, or ``""`` when it can't be resolved."""
        if card is None:
            return ""
        try:
            col = anki_compat.main_window().col
            return str(col.decks.name(card.did))
        except Exception:
            return ""

    @staticmethod
    def _card_payload(card: LookupCard) -> dict[str, Any]:
        """Flatten a :class:`LookupCard` into JSON for the clipper."""
        return {
            "note_id": card.note_id,
            "note_type": card.note_type,
            "deck": card.deck,
            "title": card.title,
            "tags": list(card.tags),
            "state": card.state,
            "interval_days": card.interval_days,
            "reps": card.reps,
            "lapses": card.lapses,
            "fields": [WordLookupPlugin._field_payload(f) for f in card.fields],
        }

    @staticmethod
    def _field_payload(field: LookupField) -> dict[str, Any]:
        return {
            "name": field.name,
            "text": field.text,
            "kind": field.kind,
            "audio": list(field.audio),
            "images": list(field.images),
        }
