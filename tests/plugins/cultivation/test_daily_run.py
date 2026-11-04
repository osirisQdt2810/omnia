"""The daily run end to end: a fake collection, a real store, real feed files.

The fake collection answers exactly the SELECTs the reader issues, so a change of query shows up
here as a failure rather than as a blank window inside Anki.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from omnia.plugins.cultivation.config import CultivationSettings
from omnia.plugins.cultivation.service import CultivationService
from omnia.plugins.cultivation.store import ANKI, CultivationStore

DAY = 86400
TODAY = date(2026, 11, 4)
CUTOFF = datetime(2026, 11, 5, 4).timestamp()  # rollover at 4 AM
NOW = CUTOFF - 6 * 3600


class FakeDb:
    def __init__(self, cards, notes, revlog):
        self.cards, self.notes, self.revlog = cards, notes, revlog
        self.statements: list[str] = []

    def all(self, sql, *args):
        self.statements.append(sql)
        assert (
            sql.lstrip().upper().startswith("SELECT")
        ), "the reader must stay read-only"
        if "FROM cards" in sql:
            return [
                (
                    c["id"],
                    c["did"],
                    0,
                    c["ord"],
                    c.get("queue", 2),
                    2,
                    0,
                    c.get("ivl", 10),
                    json.dumps(c["data"]),
                    self.notes[c["nid"]]["mid"],
                    self.notes[c["nid"]]["flds"],
                )
                for c in self.cards
            ]
        if "GROUP BY cid" in sql:
            out: dict[int, list[int]] = {}
            for rid, cid, *_ in self.revlog:
                out.setdefault(cid, []).append(rid)
            return [(cid, min(v), max(v)) for cid, v in out.items()]
        if "FROM revlog" in sql:
            return [
                (rid, cid, ease, rtype, took)
                for rid, cid, ease, rtype, took in self.revlog
            ]
        raise AssertionError(sql)


MODEL = {
    "id": 10,
    "name": "Vocab EN",
    "type": 0,
    "flds": [{"name": "Word", "ord": 0}, {"name": "Meaning", "ord": 1}],
    "tmpls": [
        {"name": "Recognition", "ord": 0, "qfmt": "{{Word}}", "afmt": "{{Meaning}}"},
        {
            "name": "Production",
            "ord": 1,
            "qfmt": "{{Meaning}} {{type:Word}}",
            "afmt": "",
        },
    ],
}


def collection(n_words=3, reviewed_days_ago=3.0, stability=10.0):
    notes, cards, revlog = {}, [], []
    for i in range(n_words):
        nid = 100 + i
        notes[nid] = {"mid": 10, "flds": f"word{i}\x1fmeaning {i}"}
        for ord_ in (0, 1):
            cid = 1000 + i * 2 + ord_
            lrt = NOW - reviewed_days_ago * DAY
            cards.append(
                {
                    "id": cid,
                    "nid": nid,
                    "did": 1,
                    "ord": ord_,
                    "data": {"s": stability, "lrt": lrt},
                }
            )
            revlog.append((int(lrt * 1000), cid, 3, 1, 8000))
    col = SimpleNamespace(
        sched=SimpleNamespace(day_cutoff=CUTOFF, today=500),
        decks=SimpleNamespace(
            all_names_and_ids=lambda: [SimpleNamespace(id=1, name="English::Vocab")]
        ),
        models=SimpleNamespace(all=lambda: [MODEL]),
        db=FakeDb(cards, notes, revlog),
    )
    return col


@pytest.fixture
def service(tmp_path):
    def make(start=TODAY - timedelta(days=60), **settings):
        s = CultivationSettings(dsa_feed_path=str(tmp_path / "feed.json"), **settings)
        return CultivationService(s, CultivationStore(tmp_path / "state.db"), start)

    return make


class TestEnglish:
    def test_score_is_the_sum_of_the_best_card_per_word(self, service):
        card = service().refresh_anki(
            collection(3, reviewed_days_ago=10, stability=10), NOW
        )
        # The typed card (μ 1.0) beats recognition (μ 0.8); R(S, S) = 0.9.
        assert card["score"] == pytest.approx(3 * 0.9)
        assert (card["label"], card["realm"], card["level"]) == ("A1", "Luyện Khí", 1)
        assert card["setup"]["templates"][1]["suggested"] == "active_recall_typed"

    def test_a_fresh_start_scores_zero_until_words_are_reviewed(self, service):
        card = service(start=TODAY).refresh_anki(
            collection(3, reviewed_days_ago=3), NOW
        )
        assert (
            card["score"] == 0.0 and card["counted_words"] == 0 and card["words"] == 3
        )
        assert card["start"] == TODAY.isoformat()

    def test_words_reviewed_on_the_start_day_count_and_are_todays_gain(self, service):
        card = service(start=TODAY).refresh_anki(
            collection(2, reviewed_days_ago=0.1), NOW
        )
        assert card["score"] > 0 and card["counted_words"] == 2
        assert card["gain"] == pytest.approx(card["score"])
        assert card["lifetime"] == pytest.approx(card["score"])

    def test_first_run_after_the_start_has_no_gain_then_measures_it(self, service):
        svc = service()
        assert svc.refresh_anki(collection(), NOW)["gain"] is None
        assert svc.refresh_anki(collection(), NOW)["gain"] == pytest.approx(0.0)

    def test_today_is_recorded_once_per_day(self, service):
        svc = service()
        svc.refresh_anki(collection(), NOW)
        svc.refresh_anki(collection(), NOW)
        assert len(svc.store.history(ANKI)) == 1

    def test_health_counts_today_and_retention(self, service):
        card = service().refresh_anki(collection(2, reviewed_days_ago=0.1), NOW)
        assert card["health"]["reviews_today"] == 4
        assert card["health"]["new_today"] == 4
        assert card["health"]["retention"] == 1.0
        assert card["streak"]["active_today"] is True

    def test_decks_outside_the_selection_do_not_count(self, service):
        assert service(decks=["Japanese"]).refresh_anki(collection(), NOW)["words"] == 0

    def test_confirmed_modality_overrides_the_suggestion(self, service):
        svc = service(modality={"Vocab EN/Production": "recognition"})
        card = svc.refresh_anki(collection(1, reviewed_days_ago=10), NOW)
        assert card["score"] == pytest.approx(
            0.8 * 0.9
        )  # recognition 0.6 < passive 0.8

    def test_the_level_follows_the_score_both_ways(self, service):
        svc = service()
        strong = svc.refresh_anki(
            collection(1600, reviewed_days_ago=1, stability=400), NOW
        )
        assert strong["label"] == "A2"
        weak = svc.refresh_anki(
            collection(1600, reviewed_days_ago=50, stability=3), NOW
        )
        assert weak["label"] == "A1"

    def test_reset_forgets_the_recorded_days(self, service):
        svc = service()
        svc.refresh_anki(collection(), NOW)
        svc.reset()
        assert svc.store.history(ANKI) == [] and not svc.store.has_snapshot()


def write_feed(path, attempts, problems=()):
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": "now",
                "attempts": attempts,
                "problems": list(problems),
                "difficulty": {},
            }
        ),
        encoding="utf-8",
    )


def attempt(lc_id, day, status="self", minutes=10):
    return {
        "lc_id": lc_id,
        "date": day.isoformat(),
        "status": status,
        "minutes": minutes,
        "wa": 0,
        "path": f"{lc_id}-{day}",
    }


PROBLEMS = [
    {
        "id": i,
        "title": f"P{i}",
        "slug": f"p{i}",
        "rating": 1700,
        "contest": "weekly-contest-1",
    }
    for i in range(1, 10)
]


class TestDsa:
    def test_missing_feed_is_reported_not_raised(self, service):
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is True and card["feed_path"].endswith("feed.json")

    def test_a_broken_feed_is_reported(self, service, tmp_path):
        (tmp_path / "feed.json").write_text("{", encoding="utf-8")
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is True and "JSON" in card["error"]

    def test_feed_is_replayed_into_a_rating(self, service, tmp_path):
        attempts = [
            attempt(1, TODAY - timedelta(days=1)),
            attempt(2, TODAY, status="copied"),
        ]
        write_feed(tmp_path / "feed.json", attempts, PROBLEMS)
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is False and card["attempts"] == 2
        assert card["label"] == "Specialist"  # ~1500 sits in 1400-1599
        assert [d["lc_id"] for d in card["debts"]] == [2]
        assert card["streak"]["days"] == 2

    def test_a_fresh_start_ignores_older_solutions(self, service, tmp_path):
        attempts = [attempt(1, TODAY - timedelta(days=30)), attempt(2, TODAY)]
        write_feed(tmp_path / "feed.json", attempts, PROBLEMS)
        card = service(start=TODAY).refresh_dsa(TODAY)
        assert card["attempts"] == 1 and len(card["history"]) == 1
        # Day one's change is measured from the initial rating.
        assert card["gain"] == pytest.approx(card["score"] - 1500)
        assert card["lifetime"] == pytest.approx(max(0.0, card["score"] - 1500))

    def test_no_attempts_since_the_start(self, service, tmp_path):
        write_feed(
            tmp_path / "feed.json", [attempt(1, TODAY - timedelta(days=30))], PROBLEMS
        )
        card = service(start=TODAY).refresh_dsa(TODAY)
        assert card["score"] == 1500 and card["attempts"] == 0 and card["gain"] == 0.0
