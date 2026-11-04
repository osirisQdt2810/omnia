"""The daily run — where the pure pieces meet the store.

:class:`CultivationService` computes one status card per domain, counting only what happened
from the plugin's start day on:

* **English**: read the collection → words reviewed since the start → vocabulary score (FSRS
  recall × card-type weight, best card per word) → today's gain against the start-of-day card
  snapshot, health, streak → level (CEFR + realm) → today's ``daily`` row.
* **DSA**: load the feed → Elo replay of the attempts since the start, with neglect decay →
  rating, today's change, streak, debts → level (tier + realm) → today's ``daily`` row.

The level is whatever today's score holds, so it rises and falls with the score. A refresh is
idempotent within a day (it rewrites today's row), so running it on every window open, review
end and sync is safe.

Imports no ``aqt``: the collection arrives as an argument, so the whole flow tests headless.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from itertools import pairwise
from typing import Any, Optional

from omnia.plugins.cultivation.config import CultivationSettings
from omnia.plugins.cultivation.dsa import DsaFeed, EloEngine, FeedError, RatingBook
from omnia.plugins.cultivation.reader import AnkiSnapshot, CollectionReader
from omnia.plugins.cultivation.realms import Level
from omnia.plugins.cultivation.store import ANKI, DSA, CultivationStore, DailyRow
from omnia.plugins.cultivation.streak import Streak, compute_streak
from omnia.plugins.cultivation.vocab import (
    VocabularyPower,
    group_words,
    start_of_day_states,
)

SECONDS_PER_DAY = 86400


class CultivationService:
    """Computes both domains' status cards.

    Args:
        settings: The plugin's settings snapshot.
        store: The plugin's state database.
        start: The counting start day (the plugin makes sure the setting holds one).
    """

    def __init__(
        self, settings: CultivationSettings, store: CultivationStore, start: date
    ) -> None:
        self.settings = settings
        self.store = store
        self.start = start

    def reset(self) -> None:
        """Forget the recorded history and snapshot (the caller moves the start day)."""
        self.store.clear()

    # -- English -----------------------------------------------------------------------------

    def refresh_anki(self, col: Any, now_s: float) -> dict[str, Any]:
        """Read the collection and record today; returns the English status card."""
        snap = CollectionReader(self.settings).read(col, now_s)
        today = snap.today
        # The start day begins at Anki's rollover, like every other day here.
        since_s = snap.day_start_s - (today - self.start).days * SECONDS_PER_DAY
        words = group_words(snap.cards)
        model = VocabularyPower(self.settings.anki.weights(), since_s=since_s)
        score = model.power(words, now_s)

        gain: Optional[float] = None
        if self.store.has_snapshot() or today == self.start:
            before = start_of_day_states(
                snap.cards, snap.day_start_s, self.store.card_snapshot()
            )
            gain = score - model.power(words, now_s, before)
        # Keep the snapshot as "the state before today": only cards untouched today move it.
        self.store.save_snapshot(
            (c.cid, c.memory)
            for c in snap.cards
            if c.memory.reviewed and (c.memory.last_review_s or 0) < snap.day_start_s
        )

        level = self.settings.anki.ladder().locate(score)
        streak = compute_streak({d for d in snap.active_days if d >= self.start}, today)
        lifetime = self.store.lifetime_before(ANKI, today, self.start) + max(
            0.0, gain or 0.0
        )
        health = asdict(snap.health) if snap.health else {}
        self.store.save_daily(
            ANKI, DailyRow(today, score, gain, lifetime, level.level, health)
        )

        card = self._card(ANKI, level, score, gain, lifetime, streak, today)
        card.update(
            health=health,
            retention_band=self._retention_band(
                snap.health.retention if snap.health else None
            ),
            counted_words=sum(1 for w in words.values() if w.counted(since_s)),
            words=len(words),
            without_fsrs=snap.without_fsrs,
            setup=self._setup_view(snap),
        )
        return card

    def _retention_band(self, retention: Optional[float]) -> str:
        if retention is None:
            return "none"
        if retention < self.settings.anki.retention_low:
            return "low"
        if retention > self.settings.anki.retention_high:
            return "high"
        return "ok"

    def _setup_view(self, snap: AnkiSnapshot) -> dict[str, Any]:
        return {
            "decks": snap.decks,
            "selected_decks": list(self.settings.decks),
            "templates": [asdict(t) for t in snap.templates],
            "note_types": [
                {**asdict(n), "fields": list(n.fields)} for n in snap.note_types
            ],
        }

    # -- DSA ---------------------------------------------------------------------------------

    def refresh_dsa(self, today: date) -> dict[str, Any]:
        """Load the feed, replay it from the start day, record today."""
        path = self.settings.feed_path()
        try:
            feed = DsaFeed.load(path)
        except (FeedError, OSError) as exc:
            return {
                "domain": DSA,
                "missing": True,
                "error": str(exc),
                "feed_path": str(path),
            }
        if feed is None:
            return {"domain": DSA, "missing": True, "error": "", "feed_path": str(path)}
        tuning = self.settings.dsa
        replay = EloEngine(tuning, RatingBook(feed, tuning)).replay(
            feed.attempts, today, start=self.start
        )
        # The rating before the first recorded day is the initial rating, so day one's change
        # and the lifetime both start from it.
        thetas = [tuning.initial_rating] + [t for _d, t in replay.history]
        gain = thetas[-1] - thetas[-2] if len(thetas) >= 2 else None
        lifetime = sum(max(0.0, b - a) for a, b in pairwise(thetas))

        level = tuning.ladder().locate(replay.theta)
        streak = compute_streak(replay.active_days, today)
        self.store.save_daily(
            DSA, DailyRow(today, replay.theta, gain, lifetime, level.level, {})
        )
        card = self._card(DSA, level, replay.theta, gain, lifetime, streak, today)
        warn_days = tuning.debt_warn_days
        card.update(
            feed_path=str(path),
            generated_at=feed.generated_at,
            attempts=len(replay.scored),
            unknown=sum(1 for s in replay.scored if s.attempt.status == "unknown"),
            rating_peak=replay.theta_peak,
            last_attempt=(
                replay.last_attempt.isoformat() if replay.last_attempt else None
            ),
            history=[
                {"day": d.isoformat(), "value": round(t, 1)} for d, t in replay.history
            ],
            debts=[
                {
                    "lc_id": d.lc_id,
                    "title": d.title,
                    "opened": d.opened.isoformat(),
                    "days": (today - d.opened).days,
                    "warn": (today - d.opened).days > warn_days,
                }
                for d in replay.debts
                if d.closed is None
            ],
            recent=[
                {
                    "lc_id": s.attempt.lc_id,
                    "title": s.attempt.title,
                    "day": s.attempt.day.isoformat(),
                    "status": s.attempt.status,
                    "rating": round(s.rating) if s.rating is not None else None,
                    "delta": round(s.theta_after - s.theta_before, 1),
                    "note": s.note,
                }
                for s in reversed(replay.scored[-15:])
            ],
        )
        return card

    # -- shared ------------------------------------------------------------------------------

    def _card(
        self,
        domain: str,
        level: Level,
        score: float,
        gain: Optional[float],
        lifetime: float,
        streak: Streak,
        today: date,
    ) -> dict[str, Any]:
        return {
            "domain": domain,
            "missing": False,
            "today": today.isoformat(),
            "start": self.start.isoformat(),
            **asdict(level),
            "score": score,
            "to_next": (
                max(0.0, level.next_threshold - score)
                if level.next_threshold is not None
                else None
            ),
            "gain": gain,
            "lifetime": lifetime,
            "streak": asdict(streak),
            "history": [
                {"day": r.day.isoformat(), "value": round(r.power, 1)}
                for r in self.store.history(domain, self.start)
            ],
        }
