"""Thiên kiếp — the trials that gate a NEW realm (spec §4.6, §5.6).

Level-ups are earned by a test, not by grinding (mastery learning: Bloom 1968). Both trials are
seeded so a trial is reproducible from its stored seed, and the tool — not the user — picks the
items, so nothing can be cherry-picked.

Pure: no ``aqt``/``anki``.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from omnia.plugins.cultivation.config import AnkiTuning, DsaTuning
from omnia.plugins.cultivation.dsa import Attempt, Problem, time_limit
from omnia.plugins.cultivation.vocab import Word, normalize_word

TYPED = "typed"
PASSIVE = "passive"


def damerau_levenshtein(a: str, b: str) -> int:
    """Optimal-string-alignment distance (adjacent transpositions count as one edit)."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(
                rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


@dataclass(frozen=True)
class AnkiTrialItem:
    """One question of the vocabulary trial."""

    word: str
    meaning: str
    card_ids: tuple[int, ...]


class AnkiTrial:
    """Samples and grades the vocabulary trial (spec §4.6)."""

    def __init__(self, tuning: AnkiTuning) -> None:
        self.t = tuning

    def eligible_words(self, words: Iterable[Word], now_s: float) -> list[Word]:
        """Reviewed at least once and NOT in the last ``trial_recent_hours``."""
        cutoff = now_s - self.t.trial_recent_hours * 3600
        out = []
        for word in words:
            last = word.last_review_s
            if word.reviewed and last is not None and last < cutoff:
                out.append(word)
        return sorted(out, key=lambda w: w.word)

    def sample(
        self,
        words: Sequence[Word],
        seed: int,
        *,
        since_ms: Optional[int],
    ) -> tuple[str, list[AnkiTrialItem]]:
        """Pick the items: half from words first reviewed since the last promotion, half from all.

        Returns ``(mode, items)``. Typed mode when at least half the picked words have a typed
        card; passive (self-graded) otherwise, which is weaker evidence and is recorded as such.
        """
        rng = random.Random(seed)
        n = min(self.t.trial_size, len(words))
        fresh = [w for w in words if since_ms and (w.first_review_ms or 0) >= since_ms]
        take_fresh = min(len(fresh), round(n * self.t.trial_new_share))
        picked = rng.sample(fresh, take_fresh) if take_fresh else []
        chosen = {w.word for w in picked}
        rest = [w for w in words if w.word not in chosen]
        picked += rng.sample(rest, min(len(rest), n - len(picked)))
        rng.shuffle(picked)
        typed = sum(1 for w in picked if w.has_typed)
        mode = TYPED if picked and typed * 2 >= len(picked) else PASSIVE
        items = [
            AnkiTrialItem(w.word, w.meaning, tuple(c.cid for c in w.cards))
            for w in picked
        ]
        return mode, items

    def grade_typed(self, expected: str, given: str) -> bool:
        """Exact after normalisation; one edit tolerated for words of ``typo_min_length``+."""
        want, got = normalize_word(expected), normalize_word(given)
        if not got:
            return False
        if want == got:
            return True
        return (
            len(want) >= self.t.typo_min_length and damerau_levenshtein(want, got) <= 1
        )

    def passed(self, correct: int) -> bool:
        return correct >= self.t.trial_pass


@dataclass(frozen=True)
class DsaTrialItem:
    """One problem of the DSA trial, and whether it has been cleared."""

    problem: Problem
    solved_on: Optional[date] = None
    note: str = ""


class DsaTrial:
    """Picks and evaluates the DSA trial (spec §5.6)."""

    def __init__(self, tuning: DsaTuning) -> None:
        self.t = tuning

    def pick(
        self,
        problems: Sequence[Problem],
        seen_ids: Iterable[int],
        target: float,
        seed: int,
    ) -> list[Problem]:
        """``trial_size`` random unseen problems within ±window of ``target``, from ≥2 contests.

        Returns [] when the pool cannot satisfy the rules (the window says why).
        """
        seen = set(seen_ids)
        pool = sorted(
            (
                p
                for p in problems
                if p.id not in seen and abs(p.rating - target) <= self.t.trial_window
            ),
            key=lambda p: p.id,
        )
        if len(pool) < self.t.trial_size:
            return []
        if len({p.contest for p in pool}) < self.t.trial_min_contests:
            return []
        rng = random.Random(seed)
        for _ in range(100):
            picked = rng.sample(pool, self.t.trial_size)
            if len({p.contest for p in picked}) >= self.t.trial_min_contests:
                return picked
        return []

    def evaluate(
        self,
        problems: Sequence[Problem],
        attempts: Iterable[Attempt],
        started: date,
    ) -> list[DsaTrialItem]:
        """Which picked problems were cleared: ``self`` and within the time limit, after start."""
        by_id: dict[int, list[Attempt]] = {}
        for attempt in attempts:
            if attempt.day >= started:
                by_id.setdefault(attempt.lc_id, []).append(attempt)
        items = []
        for problem in problems:
            limit = time_limit(problem.rating, self.t)
            cleared, note = None, ""
            for attempt in by_id.get(problem.id, []):
                if attempt.status != "self":
                    note = f"@status {attempt.status} does not count"
                elif attempt.minutes is None:
                    note = "needs @time"
                elif attempt.minutes > limit:
                    note = f"{attempt.minutes} min > {limit} min limit"
                else:
                    cleared, note = attempt.day, ""
                    break
            items.append(DsaTrialItem(problem, cleared, note))
        return items

    def deadline(self, started: date) -> date:
        return started + timedelta(days=self.t.trial_days)

    def verdict(
        self, items: Sequence[DsaTrialItem], started: date, today: date
    ) -> Optional[bool]:
        """True = passed, False = failed (window over), None = still running."""
        cleared = sum(
            1 for i in items if i.solved_on and i.solved_on <= self.deadline(started)
        )
        if cleared >= self.t.trial_pass:
            return True
        if today > self.deadline(started):
            return False
        return None


def cid_search(card_ids: Iterable[int]) -> str:
    """An Anki search for the missed words' cards — for a filtered deck (corrective phase)."""
    ids = sorted(set(card_ids))
    return f"cid:{','.join(str(i) for i in ids)}" if ids else ""
