"""The daily run and the trial flows — where the pure pieces meet the store.

:class:`CultivationService` owns one refresh per domain:

* **Anki** (spec §4): read the collection → words → power now, the §4.8 gain against the
  start-of-day snapshot, health, streak → realm state machine → today's ``daily`` row.
* **DSA** (spec §5): load the feed → Elo replay with decay → θ, today's Δθ, streak, debts →
  realm state machine → today's ``daily`` row; a running DSA trial is judged from the same feed.

Both return a JSON-ready dict — the status card the window draws. A refresh is idempotent within
a day (it rewrites today's row), so running it on every window open, review end and sync is safe.

Imports no ``aqt``: the collection arrives as an argument, so the whole flow tests headless.
"""

from __future__ import annotations

import random
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Optional

from omnia.plugins.cultivation.config import CultivationSettings
from omnia.plugins.cultivation.dsa import (
    DsaFeed,
    EloEngine,
    FeedError,
    Problem,
    RatingBook,
)
from omnia.plugins.cultivation.reader import AnkiSnapshot, CollectionReader
from omnia.plugins.cultivation.realms import (
    ANKI_LADDER,
    DSA_LADDER,
    ELIGIBLE,
    MAX_REALM,
    RealmMachine,
    RealmState,
    RealmView,
    realm_name,
)
from omnia.plugins.cultivation.store import (
    ANKI,
    DSA,
    CultivationStore,
    DailyRow,
    TrialRecord,
)
from omnia.plugins.cultivation.streak import Streak, compute_streak
from omnia.plugins.cultivation.trials import (
    PASSIVE,
    TYPED,
    AnkiTrial,
    DsaTrial,
    cid_search,
)
from omnia.plugins.cultivation.vocab import (
    VocabularyPower,
    Word,
    group_words,
    start_of_day_states,
)


class TrialError(ValueError):
    """A trial action that is not allowed right now (the message is shown as-is)."""


class CultivationService:
    """Computes both domains' status and runs their trials.

    Args:
        settings: The plugin's settings snapshot.
        store: The plugin's state database.
    """

    def __init__(self, settings: CultivationSettings, store: CultivationStore) -> None:
        self.settings = settings
        self.store = store
        self.anki_machine = RealmMachine(ANKI_LADDER, settings.demotion_buffer_days)
        self.dsa_machine = RealmMachine(DSA_LADDER, settings.demotion_buffer_days)
        self.anki_trial = AnkiTrial(settings.anki)
        self.dsa_trial = DsaTrial(settings.dsa)
        # The last Anki read, kept for the trial (which samples words) and the Setup tab.
        self._words: dict[str, Word] = {}
        self._anki_power = 0.0
        self._anki_now = 0.0
        self._feed: Optional[DsaFeed] = None
        self._dsa_theta = 0.0

    # =====================================================================================
    # Anki
    # =====================================================================================

    def refresh_anki(self, col: Any, now_s: float) -> dict[str, Any]:
        """Read the collection and record today; returns the Anki status card."""
        snap = CollectionReader(self.settings).read(col, now_s)
        words = group_words(snap.cards)
        power_model = VocabularyPower(self.settings.anki.weights())
        power = power_model.power(words, now_s)

        gain: Optional[float] = None
        if self.store.has_snapshot():
            before = start_of_day_states(
                snap.cards, snap.day_start_s, self.store.card_snapshot()
            )
            gain = power - power_model.power(words, now_s, before)
        # Keep the snapshot as "the state before today": only cards untouched today move it.
        self.store.save_snapshot(
            (c.cid, c.memory)
            for c in snap.cards
            if c.memory.reviewed and (c.memory.last_review_s or 0) < snap.day_start_s
        )

        self._words, self._anki_power, self._anki_now = words, power, now_s
        view = self._step(ANKI, self.anki_machine, power, snap.today)
        streak = compute_streak(snap.active_days, snap.today)
        lifetime = self.store.lifetime_before(ANKI, snap.today) + max(0.0, gain or 0.0)
        health = asdict(snap.health) if snap.health else {}
        self.store.save_daily(
            ANKI,
            DailyRow(
                snap.today, power, gain, lifetime, view.state.realm, view.status, health
            ),
        )
        card = self._card(ANKI, view, power, gain, lifetime, streak, snap.today)
        card.update(
            health=health,
            retention_band=self._retention_band(
                snap.health.retention if snap.health else None
            ),
            words=len(words),
            reviewed_words=sum(1 for w in words.values() if w.reviewed),
            without_fsrs=snap.without_fsrs,
            baseline=0,
            trial=self._anki_trial_view(view, snap.today),
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

    # -- Anki trial --------------------------------------------------------------------------

    def _anki_trial_view(self, view: RealmView, today: date) -> dict[str, Any]:
        running = self.store.running_trial(ANKI)
        last = self._last_finished(ANKI)
        out: dict[str, Any] = {
            "can_start": view.status == ELIGIBLE and running is None,
            "running": None,
            "last": None,
        }
        if running is not None:
            out["running"] = self._anki_trial_payload(running)
        if last is not None:
            out["last"] = {
                "passed": last.passed,
                "score": last.score,
                "total": len(last.items),
                "finished_at": (
                    last.finished_at.isoformat() if last.finished_at else None
                ),
                "search": self._missed_search(last),
            }
        return out

    def _anki_trial_payload(self, trial: TrialRecord) -> dict[str, Any]:
        """The questions as the page sees them — the answers stay in Python."""
        prompts = [
            {"prompt": item["meaning"] if trial.mode == TYPED else item["word"]}
            for item in trial.items
        ]
        return {
            "id": trial.id,
            "mode": trial.mode,
            "target_realm": trial.target_realm,
            "target_name": realm_name(trial.target_realm),
            "seed": trial.seed,
            "prompts": prompts,
            "answered": len(trial.answers),
            "correct": sum(1 for a in trial.answers if a.get("correct")),
            "pass_mark": self.settings.anki.trial_pass,
        }

    def start_anki_trial(self, today: date) -> dict[str, Any]:
        """Sample and store a new vocabulary trial (needs a refresh first)."""
        if self.store.running_trial(ANKI) is not None:
            raise TrialError("A trial is already running.")
        state = self.store.realm_state(ANKI) or self.anki_machine.seed(
            self._anki_power, today
        )
        view = self.anki_machine.step(
            state, self._anki_power, today, cooldown_until=self._cooldown_until(ANKI)
        )
        if view.status != ELIGIBLE:
            raise TrialError("Not eligible for a trial right now.")
        words = self.anki_trial.eligible_words(self._words.values(), self._anki_now)
        if not words:
            raise TrialError("No reviewed words are old enough to be tested yet.")
        seed = random.SystemRandom().randrange(1, 2**31)
        since = state.promoted_at
        since_ms = (
            int(datetime.combine(since, time()).timestamp() * 1000) if since else None
        )
        mode, items = self.anki_trial.sample(words, seed, since_ms=since_ms)
        trial_id = self.store.start_trial(
            ANKI,
            state.realm + 1,
            today,
            seed,
            mode,
            [
                {"word": i.word, "meaning": i.meaning, "cids": list(i.card_ids)}
                for i in items
            ],
        )
        return self._anki_trial_payload(self._trial(ANKI, trial_id))

    def answer_anki_trial(
        self, trial_id: int, index: int, response: str, self_grade: Optional[bool]
    ) -> dict[str, Any]:
        """Grade one question. Typed mode grades the typed word; passive mode first reveals the
        stored meaning (``self_grade`` None), then records the user's own y/n."""
        trial = self._trial(ANKI, trial_id)
        if not trial.running:
            raise TrialError("This trial is already over.")
        if index != len(trial.answers) or index >= len(trial.items):
            raise TrialError("Answer the questions in order.")
        item = trial.items[index]
        if trial.mode == TYPED:
            correct = self.anki_trial.grade_typed(item["word"], response)
        elif self_grade is None:
            return {
                "reveal": item["meaning"] or "(no meaning field)",
                "word": item["word"],
            }
        else:
            correct = bool(self_grade)
        answers = [*trial.answers, {"response": response, "correct": correct}]
        self.store.save_answers(trial.id, answers)
        done = len(answers) == len(trial.items)
        return {
            "correct": correct,
            "expected": item["word"] if trial.mode == TYPED else item["meaning"],
            "answered": len(answers),
            "score": sum(1 for a in answers if a["correct"]),
            "done": done,
        }

    def finish_anki_trial(self, trial_id: int, today: date) -> dict[str, Any]:
        """Close a fully answered trial: promote on a pass, start the cooldown on a fail."""
        trial = self._trial(ANKI, trial_id)
        if len(trial.answers) < len(trial.items):
            raise TrialError("Answer every question first.")
        score = sum(1 for a in trial.answers if a.get("correct"))
        passed = self.anki_trial.passed(score)
        self._close_trial(
            ANKI, self.anki_machine, trial, passed, score, self._anki_power, today
        )
        return {
            "passed": passed,
            "score": score,
            "total": len(trial.items),
            "search": self._missed_search(self._trial(ANKI, trial_id)),
        }

    def abandon_trial(self, domain: str, trial_id: int, today: date) -> None:
        """Give up a running trial — it counts as a fail (and starts the cooldown)."""
        trial = self._trial(domain, trial_id)
        if trial.running:
            score = sum(1 for a in trial.answers if a.get("correct"))
            self.store.finish_trial(trial.id, today, score, False)

    @staticmethod
    def _missed_search(trial: TrialRecord) -> str:
        if trial.domain != ANKI or trial.passed:
            return ""
        missed: list[int] = []
        for i, item in enumerate(trial.items):
            ok = i < len(trial.answers) and trial.answers[i].get("correct")
            if not ok:
                missed.extend(item.get("cids", []))
        return cid_search(missed)

    # =====================================================================================
    # DSA
    # =====================================================================================

    def refresh_dsa(self, today: date) -> dict[str, Any]:
        """Load the feed, replay it, judge a running trial, record today."""
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
        self._feed = feed
        engine = EloEngine(self.settings.dsa, RatingBook(feed, self.settings.dsa))
        replay = engine.replay(feed.attempts, today)
        theta = replay.theta
        self._dsa_theta = theta
        thetas = [t for _d, t in replay.history]
        gain = thetas[-1] - thetas[-2] if len(thetas) >= 2 else None
        lifetime = sum(max(0.0, b - a) for a, b in pairwise(thetas))

        trial_view = self._judge_dsa_trial(feed, today)
        view = self._step(DSA, self.dsa_machine, theta, today)
        streak = compute_streak(replay.active_days, today)
        self.store.save_daily(
            DSA,
            DailyRow(today, theta, gain, lifetime, view.state.realm, view.status, {}),
        )
        card = self._card(DSA, view, theta, gain, lifetime, streak, today)
        warn_days = self.settings.dsa.debt_warn_days
        recent = [s for s in replay.scored][-15:]
        card.update(
            feed_path=str(path),
            generated_at=feed.generated_at,
            attempts=len(feed.attempts),
            unknown=sum(1 for a in feed.attempts if a.status == "unknown"),
            theta_peak=replay.theta_peak,
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
                for s in reversed(recent)
            ],
            trial=dict(
                trial_view,
                can_start=view.status == ELIGIBLE and not trial_view["running"],
            ),
        )
        return card

    def _judge_dsa_trial(self, feed: DsaFeed, today: date) -> dict[str, Any]:
        running = self.store.running_trial(DSA)
        out: dict[str, Any] = {"running": None, "last": None}
        if running is not None:
            problems = [Problem(**p) for p in running.items]
            items = self.dsa_trial.evaluate(problems, feed.attempts, running.started_at)
            verdict = self.dsa_trial.verdict(items, running.started_at, today)
            cleared = sum(1 for i in items if i.solved_on)
            if verdict is not None:
                self._close_trial(
                    DSA,
                    self.dsa_machine,
                    running,
                    verdict,
                    cleared,
                    self._dsa_theta,
                    today,
                )
            else:
                out["running"] = {
                    "id": running.id,
                    "target_realm": running.target_realm,
                    "target_name": realm_name(running.target_realm),
                    "started_at": running.started_at.isoformat(),
                    "deadline": self.dsa_trial.deadline(running.started_at).isoformat(),
                    "cleared": cleared,
                    "pass_mark": self.settings.dsa.trial_pass,
                    "seed": running.seed,
                    "items": [
                        {
                            "id": i.problem.id,
                            "title": i.problem.title,
                            "url": i.problem.url,
                            "rating": round(i.problem.rating),
                            "contest": i.problem.contest,
                            "solved_on": (
                                i.solved_on.isoformat() if i.solved_on else None
                            ),
                            "note": i.note,
                        }
                        for i in items
                    ],
                }
        last = self._last_finished(DSA)
        if last is not None:
            out["last"] = {
                "passed": last.passed,
                "score": last.score,
                "total": len(last.items),
                "finished_at": (
                    last.finished_at.isoformat() if last.finished_at else None
                ),
            }
        return out

    def start_dsa_trial(self, today: date) -> dict[str, Any]:
        """Pick the five problems (needs a DSA refresh first)."""
        if self._feed is None:
            raise TrialError("No DSA feed yet — run /commit-progress in dsa-journey.")
        if self.store.running_trial(DSA) is not None:
            raise TrialError("A trial is already running.")
        state = self.store.realm_state(DSA) or self.dsa_machine.seed(
            self._dsa_theta, today
        )
        view = self.dsa_machine.step(
            state, self._dsa_theta, today, cooldown_until=self._cooldown_until(DSA)
        )
        if view.status != ELIGIBLE or view.next_threshold is None:
            raise TrialError("Not eligible for a trial right now.")
        seed = random.SystemRandom().randrange(1, 2**31)
        picked = self.dsa_trial.pick(
            self._feed.problems,
            {a.lc_id for a in self._feed.attempts},
            view.next_threshold,
            seed,
        )
        if not picked:
            raise TrialError(
                "Not enough unseen rated problems near this threshold "
                "(the ratings list may be missing from the feed)."
            )
        self.store.start_trial(
            DSA, state.realm + 1, today, seed, "dsa", [asdict(p) for p in picked]
        )
        return self.refresh_dsa(today)

    # =====================================================================================
    # Shared
    # =====================================================================================

    def _step(
        self, domain: str, machine: RealmMachine, power: float, today: date
    ) -> RealmView:
        state = self.store.realm_state(domain)
        if state is None:
            state = machine.seed(power, today)
        view = machine.step(
            state, power, today, cooldown_until=self._cooldown_until(domain)
        )
        self.store.save_realm_state(domain, view.state)
        return view

    def _cooldown_until(self, domain: str) -> Optional[date]:
        failed = self.store.last_failure(domain)
        if failed is None:
            return None
        return failed + timedelta(days=self.settings.trial_cooldown_days)

    def _trial(self, domain: str, trial_id: int) -> TrialRecord:
        trial = next((t for t in self.store.trials(domain) if t.id == trial_id), None)
        if trial is None:
            raise TrialError("Unknown trial.")
        return trial

    def _last_finished(self, domain: str) -> Optional[TrialRecord]:
        done = [t for t in self.store.trials(domain) if not t.running]
        return done[-1] if done else None

    def _close_trial(
        self,
        domain: str,
        machine: RealmMachine,
        trial: TrialRecord,
        passed: bool,
        score: int,
        power: float,
        today: date,
    ) -> None:
        self.store.finish_trial(trial.id, today, score, passed)
        if passed:
            state = self.store.realm_state(domain) or machine.seed(power, today)
            self.store.save_realm_state(domain, machine.pass_trial(state, power, today))

    def _card(
        self,
        domain: str,
        view: RealmView,
        power: float,
        gain: Optional[float],
        lifetime: float,
        streak: Streak,
        today: date,
    ) -> dict[str, Any]:
        state: RealmState = view.state
        nxt = state.realm + 1 if state.realm < MAX_REALM else None
        return {
            "domain": domain,
            "missing": False,
            "today": today.isoformat(),
            "realm": state.realm,
            "realm_name": realm_name(state.realm),
            "next_realm": nxt,
            "next_name": realm_name(nxt) if nxt else None,
            "peak_realm": state.peak_realm,
            "provisional": state.provisional,
            "power": power,
            "next_threshold": view.next_threshold,
            "to_next": (
                max(0.0, view.next_threshold - power) if view.next_threshold else None
            ),
            "progress": view.progress,
            "status": view.status,
            "days_until_demotion": view.days_until_demotion,
            "cooldown_until": (
                view.cooldown_until.isoformat() if view.cooldown_until else None
            ),
            "gain": gain,
            "lifetime": lifetime,
            "streak": asdict(streak),
            "history": [
                {"day": r.day.isoformat(), "value": round(r.power, 1)}
                for r in self.store.history(domain)
            ],
        }


__all__ = ["PASSIVE", "TYPED", "CultivationService", "TrialError"]
