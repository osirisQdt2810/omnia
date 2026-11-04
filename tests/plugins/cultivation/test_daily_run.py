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
from omnia.plugins.cultivation.service import CultivationService, TrialError
from omnia.plugins.cultivation.store import ANKI, DSA, CultivationStore

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
    def make(**settings):
        s = CultivationSettings(dsa_feed_path=str(tmp_path / "feed.json"), **settings)
        return CultivationService(s, CultivationStore(tmp_path / "state.db"))

    return make


class TestAnkiRefresh:
    def test_power_is_the_sum_of_the_best_card_per_word(self, service):
        card = service().refresh_anki(
            collection(3, reviewed_days_ago=10, stability=10), NOW
        )
        # The typed card (μ 1.0) beats recognition (μ 0.8); R(S, S) = 0.9.
        assert card["power"] == pytest.approx(3 * 0.9)
        assert card["words"] == 3 and card["realm"] == 1
        assert card["setup"]["templates"][1]["suggested"] == "active_recall_typed"

    def test_first_run_has_no_gain_then_a_later_day_measures_it(self, service):
        svc = service()
        assert svc.refresh_anki(collection(), NOW)["gain"] is None
        assert svc.refresh_anki(collection(), NOW)["gain"] == pytest.approx(0.0)

    def test_today_is_recorded_once_per_day(self, service, tmp_path):
        svc = service()
        svc.refresh_anki(collection(), NOW)
        svc.refresh_anki(collection(), NOW)
        assert len(svc.store.history(ANKI)) == 1

    def test_health_counts_today_and_retention(self, service):
        col = collection(2, reviewed_days_ago=0.1)
        card = service().refresh_anki(col, NOW)
        assert card["health"]["reviews_today"] == 4
        assert card["health"]["new_today"] == 4
        assert card["health"]["retention"] == 1.0
        assert card["streak"]["active_today"] is True

    def test_decks_outside_the_selection_do_not_count(self, service):
        card = service(decks=["Japanese"]).refresh_anki(collection(), NOW)
        assert card["words"] == 0

    def test_confirmed_modality_overrides_the_suggestion(self, service):
        svc = service(modality={"Vocab EN/Production": "recognition"})
        card = svc.refresh_anki(collection(1, reviewed_days_ago=10), NOW)
        assert card["power"] == pytest.approx(
            0.8 * 0.9
        )  # recognition 0.6 < passive 0.8


class TestAnkiTrialFlow:
    def test_eligibility_then_a_passed_trial_promotes(self, service):
        svc = service()
        svc.store.save_realm_state(ANKI, svc.anki_machine.seed(0, TODAY))
        # Make 1,000 words strong enough to reach Trúc Cơ.
        col = collection(1200, reviewed_days_ago=2, stability=400)
        card = svc.refresh_anki(col, NOW)
        assert card["status"] == "eligible" and card["trial"]["can_start"]
        trial = svc.start_anki_trial(TODAY)
        assert trial["mode"] == "typed" and len(trial["prompts"]) == 50
        stored = svc.store.running_trial(ANKI)
        for i, item in enumerate(stored.items):
            res = svc.answer_anki_trial(trial["id"], i, item["word"], None)
            assert res["correct"]
        result = svc.finish_anki_trial(trial["id"], TODAY)
        assert result == {"passed": True, "score": 50, "total": 50, "search": ""}
        assert svc.store.realm_state(ANKI).realm == 2

    def test_a_failed_trial_gives_a_search_and_a_cooldown(self, service):
        svc = service()
        svc.store.save_realm_state(ANKI, svc.anki_machine.seed(0, TODAY))
        svc.refresh_anki(collection(1200, reviewed_days_ago=2, stability=400), NOW)
        trial = svc.start_anki_trial(TODAY)
        for i in range(50):
            svc.answer_anki_trial(trial["id"], i, "nope", None)
        result = svc.finish_anki_trial(trial["id"], TODAY)
        assert result["passed"] is False and result["search"].startswith("cid:")
        card = svc.refresh_anki(
            collection(1200, reviewed_days_ago=2, stability=400), NOW
        )
        assert card["status"] == "cooldown"
        with pytest.raises(TrialError):
            svc.start_anki_trial(TODAY)

    def test_answers_must_come_in_order(self, service):
        svc = service()
        svc.store.save_realm_state(ANKI, svc.anki_machine.seed(0, TODAY))
        svc.refresh_anki(collection(1200, reviewed_days_ago=2, stability=400), NOW)
        trial = svc.start_anki_trial(TODAY)
        with pytest.raises(TrialError):
            svc.answer_anki_trial(trial["id"], 3, "x", None)

    def test_not_eligible_refuses_to_start(self, service):
        svc = service()
        svc.refresh_anki(collection(), NOW)
        with pytest.raises(TrialError):
            svc.start_anki_trial(TODAY)


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


class TestDsaRefresh:
    def test_missing_feed_is_reported_not_raised(self, service):
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is True and card["feed_path"].endswith("feed.json")

    def test_a_broken_feed_is_reported(self, service, tmp_path):
        (tmp_path / "feed.json").write_text("{", encoding="utf-8")
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is True and "JSON" in card["error"]

    def test_feed_is_replayed_into_a_card(self, service, tmp_path):
        problems = [
            {
                "id": i,
                "title": f"P{i}",
                "slug": f"p{i}",
                "rating": 1500 + i,
                "contest": f"weekly-contest-{i % 3}",
            }
            for i in range(1, 80)
        ]
        attempts = [
            {
                "lc_id": 1,
                "date": (TODAY - timedelta(days=1)).isoformat(),
                "status": "self",
                "minutes": 10,
                "wa": 0,
                "path": "a",
            },
            {"lc_id": 2, "date": TODAY.isoformat(), "status": "copied", "path": "b"},
        ]
        write_feed(tmp_path / "feed.json", attempts, problems)
        card = service().refresh_dsa(TODAY)
        assert card["missing"] is False and card["attempts"] == 2
        assert card["realm_name"] == "Kim Đan"  # 1500 holds 1400
        assert [d["lc_id"] for d in card["debts"]] == [2]
        assert card["streak"]["days"] == 2
        assert len(card["history"]) == 2

    def test_a_dsa_trial_is_picked_and_judged_from_later_feeds(self, service, tmp_path):
        problems = [
            {
                "id": i,
                "title": f"P{i}",
                "slug": f"p{i}",
                "rating": 1600,
                "contest": f"weekly-contest-{i % 3}",
            }
            for i in range(1, 40)
        ]
        base = [
            {"lc_id": 900, "date": TODAY.isoformat(), "status": "self", "path": "x"}
        ]
        write_feed(tmp_path / "feed.json", base, problems)
        svc = service(dsa={"initial_rating": 1610})
        svc.store.save_realm_state(
            DSA, svc.dsa_machine.seed(1500, TODAY)
        )  # realm 3, peak 3
        assert svc.refresh_dsa(TODAY)["status"] == "eligible"
        card = svc.start_dsa_trial(TODAY)
        picked = [p["id"] for p in card["trial"]["running"]["items"]]
        assert len(picked) == 5
        later = TODAY + timedelta(days=2)
        solved = [
            {
                "lc_id": pid,
                "date": later.isoformat(),
                "status": "self",
                "minutes": 30,
                "path": f"s{pid}",
            }
            for pid in picked[:3]
        ]
        write_feed(tmp_path / "feed.json", base + solved, problems)
        card = svc.refresh_dsa(later)
        assert (
            card["trial"]["running"] is None and card["trial"]["last"]["passed"] is True
        )
        assert svc.store.realm_state(DSA).realm == 4
