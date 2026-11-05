"""Read the collection into the shapes the vocabulary score computes on — strictly READ-ONLY.

Every statement here is a SELECT: the plugin never modifies the Anki collection.

There is nothing to configure. Every deck counts, so whatever you study moves the score. The
plugin decides for itself which field holds a note's word (the first field whose name looks like
one, else the first field) and how much a card type proves (typing the word > recalling it >
recognising it, read off the template).

Takes the collection as an argument and imports nothing from ``aqt``, so it runs on the
collection thread inside a ``QueryOp`` and against a fake collection in tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from omnia.plugins.cultivation.config import PASSIVE_RECALL, CultivationSettings
from omnia.plugins.cultivation.memory import CardMemory, parse_card_data
from omnia.plugins.cultivation.vocab import (
    VocabCard,
    normalize_word,
    suggest_modality,
    suggest_word_field,
)

_CLOZE = 1
_SUSPENDED_OR_BURIED = (-1, -2, -3)
_DAY = 86400


@dataclass(frozen=True)
class Health:
    """The daily health line — shown, never part of the score."""

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
    health: Optional[Health] = None
    active_days: set[date] = field(default_factory=set)
    without_fsrs: int = 0


class CollectionReader:
    """Builds an :class:`AnkiSnapshot` of the whole collection."""

    def __init__(self, settings: CultivationSettings) -> None:
        self.s = settings

    def read(self, col: Any, now_s: float) -> AnkiSnapshot:
        day_start = float(col.sched.day_cutoff) - _DAY
        today = datetime.fromtimestamp(day_start).date()
        snap = AnkiSnapshot(today=today, day_start_s=day_start, now_s=now_s)
        models = {int(m["id"]): _NoteType(m) for m in col.models.all()}
        reviews = self._review_bounds(col)
        card_ids: set[int] = set()
        overdue = 0
        rows = col.db.all(
            "SELECT c.id, c.ord, c.queue, c.type, c.due, c.ivl, c.data, n.mid, n.flds "
            "FROM cards c JOIN notes n ON n.id = c.nid"
        )
        for cid, ord_, queue, ctype, due, ivl, data, mid, flds in rows:
            note_type = models.get(int(mid))
            if note_type is None:
                continue
            if queue in _SUSPENDED_OR_BURIED and not self.s.include_suspended:
                continue
            card_ids.add(int(cid))
            if ctype == 2 and queue == 2 and due < col.sched.today:
                overdue += 1
            bounds = reviews.get(int(cid))
            snap.cards.append(
                VocabCard(
                    cid=int(cid),
                    word=note_type.word(flds),
                    modality=note_type.modality(int(ord_)),
                    memory=CardMemory.from_card(
                        parse_card_data(data),
                        ivl=int(ivl or 0),
                        last_review_s=bounds[1] / 1000 if bounds else None,
                    ),
                )
            )
        snap.without_fsrs = sum(
            1 for c in snap.cards if c.memory.reviewed and not c.memory.from_fsrs
        )
        snap.health, snap.active_days = self._health(
            col, card_ids, reviews, day_start, now_s, overdue
        )
        return snap

    @staticmethod
    def _review_bounds(col: Any) -> dict[int, tuple[int, int]]:
        """cid -> (first, last) graded review in ms; manual reschedules are not reviews."""
        rows = col.db.all(
            "SELECT cid, MIN(id), MAX(id) FROM revlog WHERE ease > 0 AND type IN (0, 1, 2, 3) "
            "GROUP BY cid"
        )
        return {int(cid): (int(first), int(last)) for cid, first, last in rows}

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
        window_ms = int((now_s - self.s.anki.retention_window_days * _DAY) * 1000)
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
            offset = int((rid / 1000 - day_start) // _DAY)
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


class _NoteType:
    """One note type, with its word field and per-template card type worked out once."""

    def __init__(self, model: Mapping[str, Any]) -> None:
        fields = [
            f["name"] for f in sorted(model.get("flds", []), key=lambda f: f["ord"])
        ]
        word_field = suggest_word_field(fields)
        self._word_index = fields.index(word_field) if word_field in fields else 0
        self._cloze = model.get("type") == _CLOZE
        self._modality = {
            int(t["ord"]): suggest_modality(
                t.get("qfmt", ""), t.get("afmt", ""), word_field
            )
            for t in model.get("tmpls", [])
        }

    def word(self, flds: str) -> str:
        values = (flds or "").split("\x1f")
        return normalize_word(
            values[self._word_index] if self._word_index < len(values) else ""
        )

    def modality(self, ord_: int) -> str:
        """Cloze cards all share template 0; an unknown ordinal falls back to it as well."""
        tord = 0 if self._cloze else ord_
        return self._modality.get(tord) or self._modality.get(0) or PASSIVE_RECALL
