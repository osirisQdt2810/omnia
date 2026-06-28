"""SQLite-backed typed-answer log + a review-session tracker (port of the reference's
``typed_stats``).

Each typed-answer outcome is one row in the ``typed_answer_log`` table inside the user's
collection database; the stats panel queries it for the donut. The store is deliberately
**DB-handle-injected** so it never reaches for ``mw``: callers pass the object exposing
``.execute(sql, *args)`` / ``.scalar(sql, *args)`` (i.e. ``mw.col.db``), and a test can pass a
``sqlite3`` connection wrapped in a tiny adapter. Deck-tree lookups go through an injected
:data:`DeckProvider` for the same reason. No ``aqt``/``anki`` imports — unit-testable headless.

Result codes (mirroring the reference): ``0=empty``, ``1=good``, ``2=bad``, ``3=miss``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

# Result codes for one typed-answer outcome.
RESULT_EMPTY = 0
RESULT_GOOD = 1
RESULT_BAD = 2
RESULT_MISS = 3


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS typed_answer_log (
    id INTEGER PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    cid INTEGER NOT NULL,
    did INTEGER NOT NULL,        -- study deck at the time of review
    card_did INTEGER NOT NULL,   -- original card did
    result INTEGER NOT NULL
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_typed_answer_log_did_ts
ON typed_answer_log(did, ts_ms);
"""


def now_ms() -> int:
    """Return the current time in epoch milliseconds."""
    return int(time.time() * 1000)


class DbHandle(Protocol):
    """The narrow database surface the store needs (satisfied by ``mw.col.db``)."""

    def execute(self, sql: str, *args: Any) -> Any: ...

    def scalar(self, sql: str, *args: Any) -> Any: ...


# Returns ``[(deck_id, deck_name), ...]`` for every deck (used for the subdeck rollup).
DeckProvider = Callable[[], list[tuple[int, str]]]


class TypedAnswerLog:
    """Persist and aggregate typed-answer outcomes in the collection database.

    The database handle is injected so the class never reaches for ``mw``; an optional deck
    provider supplies ``(id, name)`` pairs for the include-subdecks rollup.
    """

    def __init__(
        self, db: DbHandle, *, deck_provider: DeckProvider | None = None
    ) -> None:
        """Initialise the store.

        Args:
            db: A handle exposing ``execute`` / ``scalar`` (e.g. ``mw.col.db``).
            deck_provider: Returns ``(deck_id, deck_name)`` pairs; required only for
                :meth:`deck_descendants_by_prefix` (include-subdecks). When absent, the
                rollup falls back to the single requested deck.
        """
        self._db = db
        self._deck_provider = deck_provider

    def ensure(self) -> None:
        """Create the table + index if they do not exist (idempotent)."""
        self._db.execute(_TABLE_SQL)
        self._db.execute(_INDEX_SQL)

    def insert_log(self, cid: int, did: int, card_did: int, result: int) -> None:
        """Append one outcome row, stamped with the current time."""
        self.ensure()
        self._db.execute(
            "INSERT INTO typed_answer_log(ts_ms, cid, did, card_did, result) "
            "VALUES (?,?,?,?,?)",
            now_ms(),
            int(cid),
            int(did),
            int(card_did),
            int(result),
        )

    def deck_descendants_by_prefix(self, did: int) -> list[int]:
        """Return ``did`` plus every deck whose name starts with ``<did's name>::``."""
        names = dict(self._deck_provider()) if self._deck_provider is not None else {}
        name = names.get(int(did))
        if not name:
            return [int(did)]
        prefix = name + "::"
        out = [int(did)]
        for deck_id, deck_name in self._deck_provider():
            if deck_name.startswith(prefix):
                out.append(int(deck_id))
        return out

    @staticmethod
    def _pct(x: int, denom: int) -> float:
        return (100.0 * x / denom) if denom else 0.0

    def query_stats(
        self, did: int, include_subdecks: bool, start_ms: int, end_ms: int
    ) -> dict[str, Any]:
        """Aggregate outcomes for ``did`` within ``[start_ms, end_ms)``.

        Returns a dict with two views — ``attempts`` (every row) and ``unique_last`` (the
        latest result per card) — each holding totals, per-code counts, and percentages.
        """
        self.ensure()

        dids = self.deck_descendants_by_prefix(did) if include_subdecks else [int(did)]
        placeholders = ",".join("?" for _ in dids)

        total_a = int(
            self._db.scalar(
                f"""
                SELECT COUNT(*) FROM typed_answer_log
                WHERE did IN ({placeholders}) AND ts_ms >= ? AND ts_ms < ?
                """,
                *dids,
                start_ms,
                end_ms,
            )
            or 0
        )

        def count_attempts(code: int) -> int:
            return int(
                self._db.scalar(
                    f"""
                    SELECT COUNT(*) FROM typed_answer_log
                    WHERE did IN ({placeholders}) AND ts_ms >= ? AND ts_ms < ?
                          AND result = ?
                    """,
                    *dids,
                    start_ms,
                    end_ms,
                    code,
                )
                or 0
            )

        a_empty = count_attempts(RESULT_EMPTY)
        a_good = count_attempts(RESULT_GOOD)
        a_bad = count_attempts(RESULT_BAD)
        a_miss = count_attempts(RESULT_MISS)

        total_u = int(
            self._db.scalar(
                f"""
                WITH filtered AS (
                  SELECT cid, ts_ms, result
                  FROM typed_answer_log
                  WHERE did IN ({placeholders}) AND ts_ms >= ? AND ts_ms < ?
                ),
                last AS (
                  SELECT cid,
                         (SELECT result FROM filtered f2
                          WHERE f2.cid = f1.cid
                          ORDER BY ts_ms DESC
                          LIMIT 1) AS last_result
                  FROM filtered f1
                  GROUP BY cid
                )
                SELECT COUNT(*) FROM last
                """,
                *dids,
                start_ms,
                end_ms,
            )
            or 0
        )

        def count_unique_last(code: int) -> int:
            return int(
                self._db.scalar(
                    f"""
                    WITH filtered AS (
                      SELECT cid, ts_ms, result
                      FROM typed_answer_log
                      WHERE did IN ({placeholders}) AND ts_ms >= ? AND ts_ms < ?
                    ),
                    last AS (
                      SELECT cid,
                             (SELECT result FROM filtered f2
                              WHERE f2.cid = f1.cid
                              ORDER BY ts_ms DESC
                              LIMIT 1) AS last_result
                      FROM filtered f1
                      GROUP BY cid
                    )
                    SELECT COUNT(*) FROM last WHERE last_result = ?
                    """,
                    *dids,
                    start_ms,
                    end_ms,
                    code,
                )
                or 0
            )

        u_empty = count_unique_last(RESULT_EMPTY)
        u_good = count_unique_last(RESULT_GOOD)
        u_bad = count_unique_last(RESULT_BAD)
        u_miss = count_unique_last(RESULT_MISS)

        return {
            "attempts": {
                "total": total_a,
                "good": a_good,
                "bad": a_bad,
                "miss": a_miss,
                "empty": a_empty,
                "p_good": self._pct(a_good, total_a),
                "p_bad": self._pct(a_bad, total_a),
                "p_miss": self._pct(a_miss, total_a),
                "p_empty": self._pct(a_empty, total_a),
            },
            "unique_last": {
                "total": total_u,
                "good": u_good,
                "bad": u_bad,
                "miss": u_miss,
                "empty": u_empty,
                "p_good": self._pct(u_good, total_u),
                "p_bad": self._pct(u_bad, total_u),
                "p_miss": self._pct(u_miss, total_u),
                "p_empty": self._pct(u_empty, total_u),
            },
        }


class SessionTracker:
    """Records when the user entered review for each deck (for the 'current session' range).

    ``now`` is injected (defaults to :func:`now_ms`) so it stays unit-testable; the plugin's
    glue passes the current deck id when ``state_did_change`` enters the review state.
    """

    def __init__(self, *, now: Callable[[], int] = now_ms) -> None:
        self._open_ms_by_did: dict[int, int] = {}
        self._now = now

    def mark_review_entered(self, did: int) -> None:
        """Record the first time review was entered for ``did`` this session."""
        self._open_ms_by_did.setdefault(int(did), self._now())

    def get_open_ms(self, did: int) -> int:
        """Return the session-open time for ``did`` (0 if review was never entered)."""
        return int(self._open_ms_by_did.get(int(did), 0))

    def clear(self) -> None:
        """Forget all recorded session-open times (on disable)."""
        self._open_ms_by_did.clear()
