"""The DSA domain: the dsa-journey feed, problem ratings and the Elo ability model (spec §5).

dsa-journey's ``scripts/cultivation_feed.py`` (run at the end of every ``/commit-progress``)
writes a JSON feed of attempts + zerotrac ratings + LeetCode difficulties. Everything here is a
pure function of that feed and the date:

* :class:`DsaFeed` — the parsed, validated feed;
* :class:`RatingBook` — a problem's rating (zerotrac, else the Easy/Medium/Hard fallback);
* :class:`EloEngine` — replays every attempt in date order (§5.3), decaying θ across neglect
  gaps (§5.4), and returns θ per day, the attempt log and the debts.

No ``aqt``/``anki``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from omnia.plugins.cultivation.config import DsaTuning

FEED_SCHEMA = 1
STATUSES = ("self", "hint", "editorial", "copied", "unknown")


class FeedError(ValueError):
    """The feed file exists but cannot be used (bad JSON, unknown schema)."""


@dataclass(frozen=True)
class Attempt:
    """One solution file from dsa-journey."""

    lc_id: int
    day: date
    status: str
    minutes: Optional[int] = None
    wa: Optional[int] = None
    title: Optional[str] = None
    path: str = ""
    source: str = ""
    topic: Optional[str] = None


@dataclass(frozen=True)
class Problem:
    """A zerotrac-rated contest problem."""

    id: int
    title: str
    slug: str
    rating: float
    contest: str
    index: str = ""

    @property
    def url(self) -> str:
        return f"https://leetcode.com/problems/{self.slug}/" if self.slug else ""


@dataclass(frozen=True)
class DsaFeed:
    """The parsed feed."""

    generated_at: str
    attempts: tuple[Attempt, ...]
    problems: tuple[Problem, ...]
    difficulty: dict[int, str]
    repo: str = ""

    @classmethod
    def parse(cls, payload: Any) -> DsaFeed:
        """Validate and convert the JSON payload; malformed rows are skipped, not fatal."""
        if not isinstance(payload, dict):
            raise FeedError("the feed is not a JSON object")
        if payload.get("schema") != FEED_SCHEMA:
            raise FeedError(
                f"feed schema {payload.get('schema')!r} is not {FEED_SCHEMA}; "
                "update Omnia or dsa-journey so both speak the same version"
            )
        attempts = []
        for row in payload.get("attempts") or []:
            try:
                status = str(row.get("status") or "unknown").lower()
                attempts.append(
                    Attempt(
                        lc_id=int(row["lc_id"]),
                        day=date.fromisoformat(str(row["date"])[:10]),
                        status=status if status in STATUSES else "unknown",
                        minutes=_opt_int(row.get("minutes")),
                        wa=_opt_int(row.get("wa")),
                        title=row.get("title"),
                        path=str(row.get("path") or ""),
                        source=str(row.get("source") or ""),
                        topic=row.get("topic"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        problems = []
        for row in payload.get("problems") or []:
            try:
                problems.append(
                    Problem(
                        id=int(row["id"]),
                        title=str(row.get("title") or ""),
                        slug=str(row.get("slug") or ""),
                        rating=float(row["rating"]),
                        contest=str(row.get("contest") or ""),
                        index=str(row.get("index") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        difficulty = {}
        for key, value in (payload.get("difficulty") or {}).items():
            try:
                difficulty[int(key)] = str(value)
            except (TypeError, ValueError):
                continue
        attempts.sort(key=lambda a: (a.day, a.path))
        return cls(
            generated_at=str(payload.get("generated_at") or ""),
            attempts=tuple(attempts),
            problems=tuple(problems),
            difficulty=difficulty,
            repo=str(payload.get("repo") or ""),
        )

    @classmethod
    def load(cls, path: Path) -> Optional[DsaFeed]:
        """Read the feed file; None when it does not exist yet."""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise FeedError(f"the feed is not valid JSON ({exc})") from exc
        return cls.parse(payload)


def _opt_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class RatingBook:
    """A problem's difficulty on the Elo scale (spec §5.2)."""

    def __init__(self, feed: DsaFeed, tuning: DsaTuning) -> None:
        self._rated = {p.id: p.rating for p in feed.problems}
        self._difficulty = feed.difficulty
        self._fallback = {
            "Easy": tuning.fallback_easy,
            "Medium": tuning.fallback_medium,
            "Hard": tuning.fallback_hard,
        }

    def rating(self, lc_id: int) -> Optional[float]:
        """zerotrac rating, else the difficulty fallback, else None (no Elo update)."""
        if lc_id in self._rated:
            return self._rated[lc_id]
        return self._fallback.get(self._difficulty.get(lc_id, ""))

    def is_contest_rated(self, lc_id: int) -> bool:
        return lc_id in self._rated


def time_limit(rating: float, tuning: DsaTuning) -> int:
    """Minutes allowed for a problem of this rating to count as fast (spec §5.3)."""
    if rating < tuning.time_limit_easy_below:
        return tuning.time_limit_easy_min
    if rating < tuning.time_limit_hard_from:
        return tuning.time_limit_medium_min
    return tuning.time_limit_hard_min


def expected(theta: float, rating: float) -> float:
    """Elo expected success on a problem rated ``rating`` (400-point logistic)."""
    return 1.0 / (1.0 + math.pow(10.0, (rating - theta) / 400.0))


@dataclass(frozen=True)
class ScoredAttempt:
    """An attempt after the replay — the attempts table of the window."""

    attempt: Attempt
    rating: Optional[float]
    s: Optional[float]
    k: Optional[float]
    w: Optional[float]
    theta_before: float
    theta_after: float
    note: str = ""


@dataclass
class Debt:
    """A ``copied`` problem not yet re-solved alone (tâm ma)."""

    lc_id: int
    opened: date
    closed: Optional[date] = None
    title: Optional[str] = None


@dataclass
class DsaReplay:
    """Everything the replay produces."""

    theta: float
    theta_peak: float
    history: list[tuple[date, float]] = field(default_factory=list)
    scored: list[ScoredAttempt] = field(default_factory=list)
    debts: list[Debt] = field(default_factory=list)
    active_days: set[date] = field(default_factory=set)
    last_attempt: Optional[date] = None


class EloEngine:
    """Replays the attempt log into θ (spec §5.3 + §5.4)."""

    def __init__(self, tuning: DsaTuning, ratings: RatingBook) -> None:
        self.t = tuning
        self.ratings = ratings

    def quality(self, attempt: Attempt, rating: float) -> tuple[float, float]:
        """``(s, w)`` for an attempt's status (spec §5.3 table)."""
        t = self.t
        if attempt.status == "self":
            fast = (
                attempt.minutes is not None
                and attempt.minutes <= time_limit(rating, t)
                and (attempt.wa or 0) <= t.fast_max_wa
            )
            return (t.s_self_fast if fast else t.s_self), 1.0
        if attempt.status == "hint":
            return t.s_hint, 1.0
        if attempt.status == "editorial":
            return t.s_editorial, 1.0
        if attempt.status == "copied":
            return t.s_copied, 1.0
        return t.legacy_unknown_s, t.legacy_unknown_weight

    def _decay(self, theta: float, peak: float) -> float:
        """One day of neglect: halve the distance to the floor every half-life (§5.4)."""
        t = self.t
        floor = t.decay_floor_base + t.decay_floor_keep * (peak - t.decay_floor_base)
        if theta <= floor:
            return theta
        return floor + (theta - floor) * math.pow(2.0, -1.0 / t.decay_half_life_days)

    def replay(
        self, attempts: Iterable[Attempt], today: date, start: Optional[date] = None
    ) -> DsaReplay:
        """Run the attempts from ``start`` on, in order, decaying across gaps up to ``today``.

        θ begins at the initial rating on ``start`` (or on the first attempt when ``start`` is
        None), and the history has one point per day from there to ``today``.
        """
        t = self.t
        attempts = sorted(
            (a for a in attempts if start is None or a.day >= start),
            key=lambda a: (a.day, a.path),
        )
        theta = t.initial_rating
        out = DsaReplay(theta=theta, theta_peak=theta)
        if not attempts and start is None:
            return out
        by_day: dict[date, list[Attempt]] = {}
        for attempt in attempts:
            by_day.setdefault(attempt.day, []).append(attempt)

        rated_updates = 0
        boost_left = 0
        last_seen: dict[int, date] = {}
        open_debts: dict[int, Debt] = {}
        last_attempt: Optional[date] = None
        day = start if start is not None else attempts[0].day
        end = max([today, *(a.day for a in attempts[-1:])])
        while day <= end:
            todays = by_day.get(day, [])
            gap = (day - last_attempt).days if last_attempt is not None else 0
            if not todays and gap > t.decay_grace_days:
                theta = self._decay(theta, out.theta_peak)
            if todays and gap >= t.boost_gap_days:
                boost_left = t.boost_attempts
            for attempt in todays:
                before = theta
                rating = self.ratings.rating(attempt.lc_id)
                prev = last_seen.get(attempt.lc_id)
                note = ""
                s = k = w = None
                if rating is None:
                    note = "unrated"
                elif prev is not None and (day - prev).days < t.resolve_gap_days:
                    note = "re-solve within a week (lifetime only)"
                else:
                    s, w = self.quality(attempt, rating)
                    if prev is not None:
                        w *= t.resolve_weight
                        note = "re-solve"
                    if boost_left > 0:
                        k = t.k_new
                        boost_left -= 1
                    elif out.theta_peak >= t.k_master_from:
                        k = t.k_master
                    elif rated_updates < t.k_new_attempts:
                        k = t.k_new
                    else:
                        k = t.k_normal
                    theta = theta + k * w * (s - expected(theta, rating))
                    rated_updates += 1
                    out.theta_peak = max(out.theta_peak, theta)
                self._track_debt(attempt, prev, open_debts, out)
                last_seen[attempt.lc_id] = day
                out.scored.append(
                    ScoredAttempt(attempt, rating, s, k, w, before, theta, note)
                )
            if todays:
                last_attempt = day
                out.active_days.add(day)
            out.history.append((day, theta))
            day += timedelta(days=1)
        out.theta = theta
        out.last_attempt = last_attempt
        return out

    def _track_debt(
        self,
        attempt: Attempt,
        prev: Optional[date],
        open_debts: dict[int, Debt],
        out: DsaReplay,
    ) -> None:
        """Open a debt on ``copied``; close it on a spaced re-solve with self/hint."""
        debt = open_debts.get(attempt.lc_id)
        if attempt.status == "copied":
            if debt is None:
                debt = Debt(attempt.lc_id, attempt.day, title=attempt.title)
                open_debts[attempt.lc_id] = debt
                out.debts.append(debt)
            return
        if (
            debt is not None
            and attempt.status in ("self", "hint")
            and (attempt.day - debt.opened).days >= self.t.resolve_gap_days
        ):
            debt.closed = attempt.day
            del open_debts[attempt.lc_id]
