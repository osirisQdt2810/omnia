"""Read the collection into the shapes the Anki domain computes on — strictly READ-ONLY.

Every statement here is a SELECT (spec §1: never modify the Anki collection). Note types and
templates come from ``col.models`` rather than the protobuf blobs the spec's §4.1 describes for
an external reader — inside Anki the API already decodes them.

Takes the collection as an argument and imports nothing from ``aqt``, so it runs on the
collection thread inside a ``QueryOp`` and against a fake collection in tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from omnia.plugins.cultivation.config import CultivationSettings
from omnia.plugins.cultivation.memory import CardMemory, parse_card_data
from omnia.plugins.cultivation.vocab import (
    VocabCard,
    clean_field,
    is_typed_template,
    normalize_word,
    suggest_meaning_field,
    suggest_modality,
    suggest_word_field,
    template_key,
)

_CLOZE = 1
_SUSPENDED_OR_BURIED = (-1, -2, -3)


@dataclass(frozen=True)
class TemplateInfo:
    """One (note type, template) pair in the selected decks — a row of the Setup tab."""

    key: str
    note_type: str
    template: str
    cards: int
    typed: bool
    suggested: str
    modality: str
    confirmed: bool


@dataclass(frozen=True)
class NoteTypeInfo:
    """A note type's fields and the word/meaning picks for it."""

    name: str
    fields: tuple[str, ...]
    word_field: Optional[str]
    meaning_field: Optional[str]
    confirmed: bool


@dataclass(frozen=True)
class Health:
    """The daily health line (spec §4.7) — shown, never part of power."""

    retention: Optional[float]
    retention_reviews: int
    new_today: int
    reviews_today: int
    minutes_today: float
    overdue: int


@dataclass
class AnkiSnapshot:
    """Everything one refresh reads from the collection."""

    today: date
    day_start_s: float
    now_s: float
    cards: list[VocabCard] = field(default_factory=list)
    templates: list[TemplateInfo] = field(default_factory=list)
    note_types: list[NoteTypeInfo] = field(default_factory=list)
    decks: list[str] = field(default_factory=list)
    health: Optional[Health] = None
    active_days: set[date] = field(default_factory=set)
    without_fsrs: int = 0


class CollectionReader:
    """Builds an :class:`AnkiSnapshot` for the configured decks."""

    def __init__(self, settings: CultivationSettings) -> None:
        self.s = settings

    def read(self, col: Any, now_s: float) -> AnkiSnapshot:
        cutoff = float(col.sched.day_cutoff)
        day_start = cutoff - 86400
        today = datetime.fromtimestamp(day_start).date()
        snap = AnkiSnapshot(today=today, day_start_s=day_start, now_s=now_s)
        deck_names = {int(d.id): d.name for d in col.decks.all_names_and_ids()}
        snap.decks = sorted(deck_names.values())
        deck_ids = self._selected_decks(deck_names)

        models = {int(m["id"]): m for m in col.models.all()}
        reviews = self._review_bounds(col)
        counts: dict[tuple[int, int], int] = {}
        card_ids: set[int] = set()
        rows = col.db.all(
            "SELECT c.id, c.did, c.odid, c.ord, c.queue, c.type, c.due, c.ivl, c.data, "
            "n.mid, n.flds FROM cards c JOIN notes n ON n.id = c.nid"
        )
        overdue = 0
        for cid, did, odid, ord_, queue, ctype, due, ivl, data, mid, flds in rows:
            if deck_ids is not None and did not in deck_ids and odid not in deck_ids:
                continue
            model = models.get(int(mid))
            if model is None:
                continue
            if queue in _SUSPENDED_OR_BURIED and not self.s.include_suspended:
                continue
            tord = 0 if model.get("type") == _CLOZE else int(ord_)
            counts[(int(mid), tord)] = counts.get((int(mid), tord), 0) + 1
            card_ids.add(int(cid))
            if ctype == 2 and queue == 2 and due < col.sched.today:
                overdue += 1
            snap.cards.append(self._card(model, tord, cid, ivl, data, flds, reviews))
        snap.without_fsrs = sum(
            1 for c in snap.cards if c.memory.reviewed and not c.memory.from_fsrs
        )
        snap.templates, snap.note_types = self._inventory(models, counts)
        snap.health, snap.active_days = self._health(
            col, card_ids, reviews, day_start, now_s, overdue
        )
        return snap

    # -- pieces --------------------------------------------------------------------------

    def _selected_decks(self, names: Mapping[int, str]) -> Optional[set[int]]:
        """Deck ids under the configured prefixes; None = every deck."""
        prefixes = [p.strip() for p in self.s.decks if p.strip()]
        if not prefixes:
            return None
        return {
            did
            for did, name in names.items()
            if any(name == p or name.startswith(p + "::") for p in prefixes)
        }

    @staticmethod
    def _review_bounds(col: Any) -> dict[int, tuple[int, int]]:
        """cid -> (first, last) graded review in ms; manual reschedules are not reviews."""
        rows = col.db.all(
            "SELECT cid, MIN(id), MAX(id) FROM revlog WHERE ease > 0 AND type IN (0, 1, 2, 3) "
            "GROUP BY cid"
        )
        return {int(cid): (int(first), int(last)) for cid, first, last in rows}

    def _field_names(self, model: Mapping[str, Any]) -> list[str]:
        return [
            f["name"] for f in sorted(model.get("flds", []), key=lambda f: f["ord"])
        ]

    def _word_field(self, model: Mapping[str, Any]) -> Optional[str]:
        fields = self._field_names(model)
        chosen = self.s.word_fields.get(model["name"])
        return chosen if chosen in fields else suggest_word_field(fields)

    def _meaning_field(self, model: Mapping[str, Any]) -> Optional[str]:
        fields = self._field_names(model)
        chosen = self.s.meaning_fields.get(model["name"])
        if chosen in fields:
            return chosen
        return suggest_meaning_field(fields, self._word_field(model))

    def _template(self, model: Mapping[str, Any], tord: int) -> Mapping[str, Any]:
        tmpls = model.get("tmpls", [])
        return next(
            (t for t in tmpls if int(t["ord"]) == tord), tmpls[0] if tmpls else {}
        )

    def _modality(
        self, model: Mapping[str, Any], tmpl: Mapping[str, Any]
    ) -> tuple[str, str]:
        """(modality in force, suggestion) — the confirmed pick wins over the suggestion."""
        suggested = suggest_modality(
            tmpl.get("qfmt", ""), tmpl.get("afmt", ""), self._word_field(model)
        )
        key = template_key(model["name"], tmpl.get("name", ""))
        return self.s.modality.get(key, suggested), suggested

    def _card(
        self,
        model: Mapping[str, Any],
        tord: int,
        cid: int,
        ivl: int,
        data: Any,
        flds: str,
        reviews: Mapping[int, tuple[int, int]],
    ) -> VocabCard:
        fields = self._field_names(model)
        values = dict(zip(fields, (flds or "").split("\x1f"), strict=False))
        tmpl = self._template(model, tord)
        modality, _ = self._modality(model, tmpl)
        bounds = reviews.get(int(cid))
        memory = CardMemory.from_card(
            parse_card_data(data),
            ivl=int(ivl or 0),
            last_review_s=bounds[1] / 1000 if bounds else None,
        )
        word_field = self._word_field(model)
        meaning_field = self._meaning_field(model)
        return VocabCard(
            cid=int(cid),
            word=normalize_word(values.get(word_field or "", "")),
            modality=modality,
            memory=memory,
            typed=is_typed_template(tmpl.get("qfmt", ""), tmpl.get("afmt", "")),
            meaning=clean_field(values.get(meaning_field or "", "")),
            first_review_ms=bounds[0] if bounds else None,
        )

    def _inventory(
        self,
        models: Mapping[int, Mapping[str, Any]],
        counts: Mapping[tuple[int, int], int],
    ) -> tuple[list[TemplateInfo], list[NoteTypeInfo]]:
        templates: list[TemplateInfo] = []
        note_types: dict[int, NoteTypeInfo] = {}
        for (mid, tord), n in sorted(counts.items()):
            model = models[mid]
            tmpl = self._template(model, tord)
            modality, suggested = self._modality(model, tmpl)
            key = template_key(model["name"], tmpl.get("name", ""))
            templates.append(
                TemplateInfo(
                    key=key,
                    note_type=model["name"],
                    template=tmpl.get("name", ""),
                    cards=n,
                    typed=is_typed_template(tmpl.get("qfmt", ""), tmpl.get("afmt", "")),
                    suggested=suggested,
                    modality=modality,
                    confirmed=key in self.s.modality,
                )
            )
            if mid not in note_types:
                note_types[mid] = NoteTypeInfo(
                    name=model["name"],
                    fields=tuple(self._field_names(model)),
                    word_field=self._word_field(model),
                    meaning_field=self._meaning_field(model),
                    confirmed=model["name"] in self.s.word_fields,
                )
        return templates, sorted(note_types.values(), key=lambda n: n.name)

    def _health(
        self,
        col: Any,
        card_ids: set[int],
        reviews: Mapping[int, tuple[int, int]],
        day_start: float,
        now_s: float,
        overdue: int,
    ) -> tuple[Health, set[date]]:
        """Retention, today's activity and the active days for the streak."""
        window_ms = int((now_s - self.s.anki.retention_window_days * 86400) * 1000)
        start_ms = int(day_start * 1000)
        passed = total = reviews_today = 0
        minutes = 0.0
        active: set[date] = set()
        first_day = datetime.fromtimestamp(day_start).date()
        for rid, cid, ease, rtype, took in col.db.all(
            "SELECT id, cid, ease, type, time FROM revlog WHERE ease > 0"
        ):
            if int(cid) not in card_ids:
                continue
            # Anki's day boundary is the rollover hour, so days are counted from today's start.
            offset = int((rid / 1000 - day_start) // 86400)
            active.add(first_day + timedelta(days=offset))
            if rid >= window_ms and rtype == 1:
                total += 1
                passed += ease >= 2
            if rid >= start_ms:
                reviews_today += 1
                minutes += (took or 0) / 60000
        new_today = sum(
            1 for cid in card_ids if cid in reviews and reviews[cid][0] >= start_ms
        )
        health = Health(
            retention=passed / total if total else None,
            retention_reviews=total,
            new_today=new_today,
            reviews_today=reviews_today,
            minutes_today=round(minutes, 1),
            overdue=overdue,
        )
        return health, active
