"""Cultivation's own state — a SQLite file under ``user_files/cultivation/`` (spec §6 State).

Not in the collection: the plugin is strictly READ-ONLY on Anki data (spec §1), and
``user_files/`` is the one add-on folder an Anki update keeps. Per machine and not synced —
the history of a day is recomputed from the collection anyway, only the realm/trial record
and the start-of-day card snapshot are genuinely local.

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
from omnia.plugins.cultivation.realms import RealmState

ANKI = "anki"
DSA = "dsa"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    domain TEXT NOT NULL, day TEXT NOT NULL, power REAL NOT NULL, gain REAL,
    lifetime REAL NOT NULL DEFAULT 0, realm INTEGER NOT NULL, status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (domain, day));
CREATE TABLE IF NOT EXISTS card_state (
    cid INTEGER PRIMARY KEY, s REAL NOT NULL, decay REAL NOT NULL, lrt REAL);
CREATE TABLE IF NOT EXISTS realm_state (
    domain TEXT PRIMARY KEY, realm INTEGER NOT NULL, peak_realm INTEGER NOT NULL,
    unstable_since TEXT, provisional INTEGER NOT NULL DEFAULT 0, promoted_at TEXT);
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, target_realm INTEGER NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, seed INTEGER NOT NULL, mode TEXT NOT NULL,
    items_json TEXT NOT NULL, answers_json TEXT NOT NULL DEFAULT '[]', score INTEGER,
    passed INTEGER);
"""


@dataclass(frozen=True)
class DailyRow:
    """One domain's numbers for one day."""

    day: date
    power: float
    gain: Optional[float]
    lifetime: float
    realm: int
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class TrialRecord:
    """A stored trial (running when ``finished_at`` is None)."""

    id: int
    domain: str
    target_realm: int
    started_at: date
    finished_at: Optional[date]
    seed: int
    mode: str
    items: list[dict[str, Any]]
    answers: list[dict[str, Any]]
    score: Optional[int]
    passed: Optional[bool]

    @property
    def running(self) -> bool:
        return self.finished_at is None


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


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

    # -- realm state ---------------------------------------------------------------------

    def realm_state(self, domain: str) -> Optional[RealmState]:
        with self._db() as db:
            row = db.execute(
                "SELECT realm, peak_realm, unstable_since, provisional, promoted_at "
                "FROM realm_state WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        return RealmState(row[0], row[1], _d(row[2]), bool(row[3]), _d(row[4]))

    def save_realm_state(self, domain: str, state: RealmState) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO realm_state VALUES (?, ?, ?, ?, ?, ?)",
                (
                    domain,
                    state.realm,
                    state.peak_realm,
                    state.unstable_since.isoformat() if state.unstable_since else None,
                    int(state.provisional),
                    state.promoted_at.isoformat() if state.promoted_at else None,
                ),
            )

    # -- daily history -------------------------------------------------------------------

    def save_daily(self, domain: str, row: DailyRow) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    domain,
                    row.day.isoformat(),
                    row.power,
                    row.gain,
                    row.lifetime,
                    row.realm,
                    row.status,
                    json.dumps(row.details),
                ),
            )

    def history(self, domain: str) -> list[DailyRow]:
        with self._db() as db:
            rows = db.execute(
                "SELECT day, power, gain, lifetime, realm, status, details_json FROM daily "
                "WHERE domain = ? ORDER BY day",
                (domain,),
            ).fetchall()
        return [
            DailyRow(
                date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5], json.loads(r[6])
            )
            for r in rows
        ]

    def lifetime_before(self, domain: str, day: date) -> float:
        """Σ max(0, gain) over the days before ``day`` (lifetime never decreases)."""
        with self._db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(MAX(gain, 0)), 0) FROM daily "
                "WHERE domain = ? AND day < ? AND gain IS NOT NULL",
                (domain, day.isoformat()),
            ).fetchone()
        return float(row[0] or 0.0)

    # -- start-of-day card snapshot (spec §4.8) ------------------------------------------

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

    # -- trials --------------------------------------------------------------------------

    def start_trial(
        self,
        domain: str,
        target_realm: int,
        started: date,
        seed: int,
        mode: str,
        items: list[dict[str, Any]],
    ) -> int:
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO trials (domain, target_realm, started_at, seed, mode, items_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    domain,
                    target_realm,
                    started.isoformat(),
                    seed,
                    mode,
                    json.dumps(items),
                ),
            )
            return int(cur.lastrowid or 0)

    def save_answers(self, trial_id: int, answers: list[dict[str, Any]]) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE trials SET answers_json = ? WHERE id = ?",
                (json.dumps(answers), trial_id),
            )

    def finish_trial(
        self, trial_id: int, finished: date, score: int, passed: bool
    ) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE trials SET finished_at = ?, score = ?, passed = ? WHERE id = ?",
                (finished.isoformat(), score, int(passed), trial_id),
            )

    def trials(self, domain: str) -> list[TrialRecord]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, domain, target_realm, started_at, finished_at, seed, mode, "
                "items_json, answers_json, score, passed FROM trials WHERE domain = ? "
                "ORDER BY id",
                (domain,),
            ).fetchall()
        return [
            TrialRecord(
                r[0],
                r[1],
                r[2],
                date.fromisoformat(r[3]),
                _d(r[4]),
                r[5],
                r[6],
                json.loads(r[7]),
                json.loads(r[8]),
                r[9],
                None if r[10] is None else bool(r[10]),
            )
            for r in rows
        ]

    def running_trial(self, domain: str) -> Optional[TrialRecord]:
        return next((t for t in self.trials(domain) if t.running), None)

    def last_failure(self, domain: str) -> Optional[date]:
        failed = [t.finished_at for t in self.trials(domain) if t.passed is False]
        return max((d for d in failed if d), default=None)
