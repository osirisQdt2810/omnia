"""Tests for the typed_accuracy feature (pure logic + the reviewer/bridge seam)."""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import types
from pathlib import Path

import pytest
from conftest import FakeCard

from omnia.core.plugin import AddonPaths, PluginContext
from omnia.core.providers import ProviderHub
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.core.reviewer.web_injector import WebInjector, build_message
from omnia.plugins.typed_accuracy import TypedAccuracyPlugin
from omnia.plugins.typed_accuracy.logic import (
    accuracy_ratio,
    decide_ease,
    result_code,
)
from omnia.plugins.typed_accuracy.store import (
    RESULT_BAD,
    RESULT_EMPTY,
    RESULT_GOOD,
    RESULT_MISS,
)

# The feature's web assets live next to the package; the StatsInjector reads from web_dir.
_WEB_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "omnia"
    / "plugins"
    / "typed_accuracy"
    / "web"
)


class _DbAdapter:
    """Wrap a ``sqlite3`` connection to expose Anki's ``.execute`` / ``.scalar`` surface."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        return self._conn.execute(sql, args)

    def scalar(self, sql: str, *args: object):
        row = self._conn.execute(sql, args).fetchone()
        return row[0] if row else None


class TestAccuracyLogic:
    def test_accuracy_ratio(self):
        assert accuracy_ratio(0, 0, 0) == 0.0
        assert accuracy_ratio(8, 1, 1) == 0.8

    def test_decide_ease_pass_good_fail_hard(self):
        assert decide_ease(0.9, 0.7, "good") == 3
        assert decide_ease(0.9, 0.7, "easy") == 4
        assert decide_ease(0.5, 0.7, "good") == 2  # fail -> Hard

    def test_decide_ease_no_stages_nothing_on_pass_but_hard_on_fail(self):
        assert decide_ease(0.9, 0.7, "no") is None  # pass -> stage nothing
        assert decide_ease(0.0, 0.7, "no") == 2  # fail still forces Hard

    def test_result_code_precedence(self):
        assert result_code(True, True, True) == RESULT_MISS  # miss wins
        assert result_code(True, True, False) == RESULT_BAD  # bad over good
        assert result_code(True, False, False) == RESULT_GOOD
        assert result_code(False, False, False) == RESULT_EMPTY


def _context(settings, user_files: Path | None = None):
    uf = user_files or Path(tempfile.mkdtemp())
    return PluginContext(
        plugin_id="typed_accuracy",
        settings=settings,
        log=logging.getLogger("omnia.test"),
        ease=EasePipeline(),
        web=WebInjector(),
        providers=ProviderHub(),
        paths=AddonPaths(uf, _WEB_DIR, uf),
        config=None,  # not exercised by these seam tests
        reload_self=lambda: None,
    )


@pytest.fixture
def fake_mw(monkeypatch):
    """A fake ``mw`` with an in-memory collection DB + a reviewer holding one card."""
    import aqt

    card = FakeCard(id=42)
    card.did = 7
    conn = sqlite3.connect(":memory:")
    db = _DbAdapter(conn)
    decks = types.SimpleNamespace(
        get_current_id=lambda: 7,
        all=lambda: [{"id": 7, "name": "Spanish"}],
    )
    col = types.SimpleNamespace(db=db, decks=decks)
    mw = types.SimpleNamespace(reviewer=types.SimpleNamespace(card=card), col=col)
    monkeypatch.setattr(aqt, "mw", mw)
    return types.SimpleNamespace(mw=mw, card=card, conn=conn, db=db)


def _logged_rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT cid, did, card_did, result FROM typed_answer_log ORDER BY id"
    ).fetchall()


class TestTypedAccuracyPlugin:
    def test_pass_grades_good_via_pipeline_and_logs(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="good"))
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)

        handled, result = ctx.web._router.dispatch(
            build_message(
                "typed_accuracy",
                "rated",
                {"ratio": 0.95, "hasGood": True, "hasBad": False, "hasMiss": False},
            ),
            None,
        )
        assert handled is True and result["ok"] is True
        assert (
            ctx.ease.compute_ease(fake_mw.card, 1) == 3
        )  # staged Good overrides request
        # The result was logged with the study deck and the card's deck.
        assert _logged_rows(fake_mw.conn) == [(42, 7, 7, RESULT_GOOD)]

    def test_fail_grades_hard(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="easy"))
        TypedAccuracyPlugin().on_enable(ctx)
        ctx.web._router.dispatch(
            build_message(
                "typed_accuracy",
                "rated",
                {"ratio": 0.4, "hasGood": True, "hasBad": True, "hasMiss": False},
            ),
            None,
        )
        assert ctx.ease.compute_ease(fake_mw.card, 3) == 2  # Hard
        assert _logged_rows(fake_mw.conn) == [(42, 7, 7, RESULT_BAD)]

    def test_empty_no_markup_forces_hard_and_logs_empty(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="good"))
        TypedAccuracyPlugin().on_enable(ctx)
        # ratio 0 with no markup spans (the JS empty case).
        ctx.web._router.dispatch(
            build_message(
                "typed_accuracy",
                "rated",
                {"ratio": 0.0, "hasGood": False, "hasBad": False, "hasMiss": False},
            ),
            None,
        )
        assert ctx.ease.compute_ease(fake_mw.card, 3) == 2  # forced Hard
        assert _logged_rows(fake_mw.conn) == [(42, 7, 7, RESULT_EMPTY)]

    def test_auto_answer_no_stages_nothing_but_logs(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings(threshold=0.7, pass_ease="no"))
        TypedAccuracyPlugin().on_enable(ctx)
        ctx.web._router.dispatch(
            build_message(
                "typed_accuracy",
                "rated",
                {"ratio": 0.95, "hasGood": True, "hasBad": False, "hasMiss": False},
            ),
            None,
        )
        # Pass with auto_answer=no: nothing staged, the user's press stands.
        assert ctx.ease.compute_ease(fake_mw.card, 4) == 4
        # But the result is still logged.
        assert _logged_rows(fake_mw.conn) == [(42, 7, 7, RESULT_GOOD)]

    def test_other_card_not_affected(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        TypedAccuracyPlugin().on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95, "hasGood": True}),
            None,
        )
        other = FakeCard(id=999)
        assert ctx.ease.compute_ease(other, 3) == 3  # no pending ease for this card

    def test_question_clears_stale_pending(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95, "hasGood": True}),
            None,
        )
        plugin._on_question(fake_mw.card)  # showing the question wipes staged ease
        assert ctx.ease.compute_ease(fake_mw.card, 3) == 3

    def test_query_op_returns_attempts_and_unique(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        TypedAccuracyPlugin().on_enable(ctx)
        # Log two attempts for the same card; unique_last should collapse to the latest.
        for ratio, good, bad in ((0.4, True, True), (0.95, True, False)):
            ctx.web._router.dispatch(
                build_message(
                    "typed_accuracy",
                    "rated",
                    {"ratio": ratio, "hasGood": good, "hasBad": bad, "hasMiss": False},
                ),
                None,
            )

        _, res = ctx.web._router.dispatch(
            build_message(
                "typed_accuracy",
                "query",
                {"did": 7, "includeSubdecks": False, "startMs": 0, "endMs": 10**18},
            ),
            None,
        )
        assert res["ok"] is True
        assert res["data"]["attempts"]["total"] == 2
        assert res["data"]["unique_last"]["total"] == 1
        assert res["data"]["unique_last"]["good"] == 1  # latest was good

    def test_get_current_did_op(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        TypedAccuracyPlugin().on_enable(ctx)
        _, res = ctx.web._router.dispatch(
            build_message("typed_accuracy", "get_current_did", {}), None
        )
        assert res == {"ok": True, "did": 7}

    def test_session_open_ms_recorded_on_state_change(self, fake_mw):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        # No session yet.
        _, before = ctx.web._router.dispatch(
            build_message("typed_accuracy", "get_session_open_ms", {"did": 7}), None
        )
        assert before == {"ok": True, "openMs": 0}
        # Entering review records the open time.
        plugin._on_state_change("review", "deckBrowser")
        _, after = ctx.web._router.dispatch(
            build_message("typed_accuracy", "get_session_open_ms", {"did": 7}), None
        )
        assert after["ok"] is True and after["openMs"] > 0

    def test_disable_fully_tears_down(self, fake_mw, gui_hooks):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        ctx = _context(TypedAccuracySettings())
        plugin = TypedAccuracyPlugin()
        plugin.on_enable(ctx)
        ctx.web._router.dispatch(
            build_message("typed_accuracy", "rated", {"ratio": 0.95, "hasGood": True}),
            None,
        )
        assert plugin._pending  # staged
        plugin.on_disable(ctx)
        assert ctx.ease.compute_ease(fake_mw.card, 3) == 3  # transformer removed
        assert not plugin._pending  # cleared
        assert ctx.web.collect_js("answer") == ""  # asset removed
        # Every handler op removed.
        for op in (
            "rated",
            "query",
            "get_current_did",
            "get_session_open_ms",
            "dbg",
        ):
            handled, _ = ctx.web._router.dispatch(
                build_message("typed_accuracy", op, {}), None
            )
            assert handled is False
        assert gui_hooks.reviewer_did_show_question.count() == 0
        assert gui_hooks.state_did_change.count() == 0
        assert gui_hooks.webview_did_inject_style_into_page.count() == 0


class TestStatsInjector:
    def test_inject_evals_assets_into_webview(self, tmp_path):
        from omnia.plugins.typed_accuracy.stats_injector import StatsInjector

        evals: list[str] = []
        webview = types.SimpleNamespace(eval=evals.append)
        StatsInjector(_WEB_DIR, logging.getLogger("omnia.test")).inject(webview)
        joined = "\n".join(evals)
        assert "__TA_HTML_TEMPLATE" in joined  # template stashed
        assert "ta-card" in joined  # html template content present
        assert "__TA_BOOTED" in joined  # panel JS ran

    def test_missing_assets_logged_not_raised(self, tmp_path):
        from omnia.plugins.typed_accuracy.stats_injector import StatsInjector

        webview = types.SimpleNamespace(eval=lambda _js: None)
        # tmp_path has no assets: inject must swallow the OSError.
        StatsInjector(tmp_path, logging.getLogger("omnia.test")).inject(webview)
