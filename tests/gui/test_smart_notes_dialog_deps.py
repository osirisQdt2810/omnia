"""Tests for the SmartNotesDialog prompt→graph dependency sync (Feature 1) glue.

The off-thread classify + main-thread reconcile + push live on the dialog. The dialog's
``WebDialog`` base needs a real Qt stack, so these tests exercise ONLY the pure-ish glue
methods (``_plan_dep_classification`` / ``_reconcile_rows``) on an instance built WITHOUT
running ``__init__`` — they touch just ``self._deps_memo`` plus pure omnia imports, so they
run headless. The extra ``aqt`` symbols ``web_dialog`` imports at module load are stubbed
here before importing the dialog.
"""

from __future__ import annotations

import sys
import types

# --- stub the extra aqt symbols dialog.py / web_dialog.py import at module load ----------
_theme_mod = types.ModuleType("aqt.theme")
_theme_mod.theme_manager = types.SimpleNamespace(night_mode=False)
sys.modules.setdefault("aqt.theme", _theme_mod)

_qt = sys.modules.get("aqt.qt") or types.ModuleType("aqt.qt")
for _name in ("QDialog", "QVBoxLayout", "QWebEngineView", "QWidget"):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, type(_name, (), {}))
sys.modules["aqt.qt"] = _qt
import aqt  # noqa: E402  (the conftest stub package)

aqt.qt = _qt
aqt.theme = _theme_mod

_webview_mod = types.ModuleType("aqt.webview")
_webview_mod.AnkiWebView = type("AnkiWebView", (), {})
sys.modules.setdefault("aqt.webview", _webview_mod)
aqt.webview = _webview_mod

from omnia.gui.smart_notes.dialog import SmartNotesDialog, _DepPlan  # noqa: E402


def _dialog() -> SmartNotesDialog:
    """A dialog instance with only the deps memo set (no Qt ``__init__``)."""
    dlg = object.__new__(SmartNotesDialog)
    dlg._deps_memo = {}
    return dlg


def _row(field: str, prompt: str, depends_on=None) -> dict:
    return {"field": field, "prompt": prompt, "depends_on": depends_on or []}


class TestPlanDepClassification:
    def test_only_new_refs_become_uncached_items(self):
        dlg = _dialog()
        rows = [
            _row(
                "Example",
                "Use {{Kanji}} and {{Reading}}.",
                [{"field": "Reading", "kind": "soft", "auto": False}],
            )
        ]
        plan = dlg._plan_dep_classification("Vocab", "Kanji", rows)
        # Reading already has an entry → not new; only Kanji needs classifying.
        assert plan.uncached_items == [
            ("Example", "Use {{Kanji}} and {{Reading}}.", ["Kanji"])
        ]
        assert plan.cached == {}

    def test_row_with_no_new_refs_is_omitted(self):
        dlg = _dialog()
        rows = [_row("Example", "Plain prompt, no refs.")]
        plan = dlg._plan_dep_classification("Vocab", "Kanji", rows)
        assert plan == _DepPlan()

    def test_memo_hit_skips_the_llm_item(self):
        dlg = _dialog()
        # Pre-seed the memo for this exact (field, prompt) — a prior classify this session.
        dlg._deps_memo[("Vocab", "Kanji", "Example", "Use {{Kanji}}.")] = {
            "Kanji": "hard"
        }
        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = dlg._plan_dep_classification("Vocab", "Kanji", rows)
        assert plan.uncached_items == []  # no LLM call needed
        assert plan.cached == {"Example": {"Kanji": "hard"}}


class TestReconcileRows:
    def test_fresh_classification_adds_auto_edges(self):
        dlg = _dialog()
        from omnia.plugins.smart_notes.authoring import EdgeKinding

        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = _DepPlan(
            uncached_items=[("Example", "Use {{Kanji}}.", ["Kanji"])], cached={}
        )
        classified = {"Example": (EdgeKinding(field="Kanji", kind="soft"),)}
        items = dlg._reconcile_rows("Vocab", "Kanji", rows, plan, classified)
        assert items == [
            {
                "field": "Example",
                "depends_on": [{"field": "Kanji", "kind": "soft", "auto": True}],
            }
        ]
        # The fresh verdict is memoised by (field, prompt) for a later re-save.
        assert dlg._deps_memo[("Vocab", "Kanji", "Example", "Use {{Kanji}}.")] == {
            "Kanji": "soft"
        }

    def test_memo_cached_verdicts_are_used_without_classification(self):
        dlg = _dialog()
        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = _DepPlan(uncached_items=[], cached={"Example": {"Kanji": "hard"}})
        items = dlg._reconcile_rows("Vocab", "Kanji", rows, plan, {})
        assert items == [
            {
                "field": "Example",
                "depends_on": [{"field": "Kanji", "kind": "hard", "auto": True}],
            }
        ]

    def test_existing_kind_is_kept_disjoint_from_classification(self):
        # Reading already user-set soft; reconcile keeps it even though the classifier said hard.
        dlg = _dialog()
        from omnia.plugins.smart_notes.authoring import EdgeKinding

        rows = [
            _row(
                "Example",
                "Use {{Kanji}} and {{Reading}}.",
                [{"field": "Reading", "kind": "soft", "auto": False}],
            )
        ]
        plan = _DepPlan(
            uncached_items=[("Example", rows[0]["prompt"], ["Kanji"])], cached={}
        )
        classified = {
            "Example": (
                EdgeKinding(field="Kanji", kind="hard"),
                EdgeKinding(field="Reading", kind="hard"),
            )
        }
        items = dlg._reconcile_rows("Vocab", "Kanji", rows, plan, classified)
        deps = {d["field"]: d for d in items[0]["depends_on"]}
        assert deps["Reading"] == {"field": "Reading", "kind": "soft", "auto": False}
        assert deps["Kanji"] == {"field": "Kanji", "kind": "hard", "auto": True}

    def test_vanished_auto_edge_is_dropped_with_no_classification(self):
        dlg = _dialog()
        rows = [
            _row(
                "Example",
                "No references anymore.",
                [{"field": "Kanji", "kind": "hard", "auto": True}],
            )
        ]
        # No new refs → no classify; reconcile still drops the stale auto edge.
        plan = _DepPlan()
        items = dlg._reconcile_rows("Vocab", "Kanji", rows, plan, {})
        assert items == [{"field": "Example", "depends_on": []}]


class TestClassifyDepsThreadRouting:
    """Guards the single most dangerous invariant: an off-main-thread eval_js is a native Qt
    segfault, so the page push must happen ONLY in the run_in_background success callback (main
    thread), NEVER inside the worker. A refactor that moved the push into the worker would pass
    every other test and crash users — this catches it."""

    def test_push_is_in_the_success_callback_not_the_worker(self, monkeypatch):
        from conftest import FakeLLMProvider

        from omnia.core import anki_compat

        captured: dict = {}

        def fake_run(op, *, on_success, on_failure=None, parent=None, label=None):
            captured["op"] = op
            captured["on_success"] = on_success

        monkeypatch.setattr(anki_compat, "run_in_background", fake_run)

        dlg = _dialog()
        evals: list[str] = []
        dlg.eval_js = lambda js: evals.append(js)
        dlg._build_hub = lambda: types.SimpleNamespace(
            llm=lambda: FakeLLMProvider(
                text='{"Example": [{"field": "Kanji", "kind": "hard"}]}'
            )
        )

        dlg._on_classify_deps(
            {
                "note_type": "Vocab",
                "base_field": "Kanji",
                "rows": [_row("Example", "Use {{Kanji}}.")],
            }
        )
        # The worker (op) computes off-thread — it must NOT touch the WebView.
        result = captured["op"]()
        assert evals == []
        # The success callback (main thread) is the only place that pushes.
        captured["on_success"](result)
        assert any("__snDepsResult" in js for js in evals)
