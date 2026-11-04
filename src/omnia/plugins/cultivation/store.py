"""Cultivation's own state — a SQLite file under ``user_files/cultivation/``.

Not in the collection: the plugin is strictly READ-ONLY on Anki data, and ``user_files/`` is the
one add-on folder an Anki update keeps. Two things live here:

* ``daily`` — one row per domain per day (score, gain, lifetime, level) for the history chart;
* ``card_state`` — each card's memory as last seen before today, for today's English gain.

Both are per machine and disposable: "Start over" clears them, and a lost file only costs the
chart its past days.

A short-lived connection per call: the refresh runs on Anki's collection thread and the window
on the main thread, and sqlite3 connections must not cross threads.

Pure stdlib; no ``aqt``/``anki``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from omnia.plugins.cultivation.memory import CardMemory

ANKI = "anki"
DSA = "dsa"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    domain TEXT NOT NULL, day TEXT NOT NULL, power REAL NOT NULL, gain REAL,
    lifetime REAL NOT NULL DEFAULT 0, level INTEGER NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (domain, day));
CREATE TABLE IF NOT EXISTS card_state (
    cid INTEGER PRIMARY KEY, s REAL NOT NULL, decay REAL NOT NULL, lrt REAL);
"""


@dataclass(frozen=True)
class DailyRow:
    """One domain's numbers for one day."""

    day: date
    power: float
    gain: Optional[float]
    lifetime: float
    level: int
    details: dict[str, Any]


class CultivationStore:
    """Read/write access to ``state.db``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def clear(self) -> None:
        """Forget every recorded day and the card snapshot — "Start over"."""
        with self._db() as db:
            db.execute("DELETE FROM daily")
            db.execute("DELETE FROM card_state")

    # -- daily history -------------------------------------------------------------------

    def save_daily(self, domain: str, row: DailyRow) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    domain,
                    row.day.isoformat(),
                    row.power,
                    row.gain,
                    row.lifetime,
                    row.level,
                    json.dumps(row.details),
                ),
            )

    def history(self, domain: str, since: Optional[date] = None) -> list[DailyRow]:
        """Recorded days, oldest first (from ``since`` on when given)."""
        with self._db() as db:
            rows = db.execute(
                "SELECT day, power, gain, lifetime, level, details_json FROM daily "
                "WHERE domain = ? AND day >= ? ORDER BY day",
                (domain, since.isoformat() if since else ""),
            ).fetchall()
        return [
            DailyRow(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], json.loads(r[5]))
            for r in rows
        ]

    def lifetime_before(
        self, domain: str, day: date, since: Optional[date] = None
    ) -> float:
        """Σ max(0, gain) over the recorded days in ``[since, day)`` — it never decreases."""
        with self._db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(MAX(gain, 0)), 0) FROM daily "
                "WHERE domain = ? AND day < ? AND day >= ? AND gain IS NOT NULL",
                (domain, day.isoformat(), since.isoformat() if since else ""),
            ).fetchone()
        return float(row[0] or 0.0)

    # -- start-of-day card snapshot ------------------------------------------------------

    def card_snapshot(self) -> dict[int, CardMemory]:
        with self._db() as db:
            rows = db.execute("SELECT cid, s, decay, lrt FROM card_state").fetchall()
        return {r[0]: CardMemory(r[1], r[2], r[3]) for r in rows}

    def has_snapshot(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM card_state LIMIT 1").fetchone() is not None

    def save_snapshot(self, states: Iterable[tuple[int, CardMemory]]) -> None:
        with self._db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO card_state VALUES (?, ?, ?, ?)",
                [(cid, m.stability, m.decay, m.last_review_s) for cid, m in states],
            )
