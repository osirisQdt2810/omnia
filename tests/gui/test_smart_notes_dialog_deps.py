"""Tests for the Smart Notes controllers' prompt↔graph dependency sync glue.

The off-thread classify + main-thread reconcile + push live on the
:class:`~omnia.gui.smart_notes.dialogs.controllers.graph.GraphController` (Features 1 & 2); the save
cycle guard lives on the :class:`~omnia.gui.smart_notes.dialogs.controllers.config.ConfigController`.
Both take a shared ``SmartNotesContext``; here we hand them a tiny fake context (a
``SimpleNamespace`` exposing just ``eval_js`` / ``build_hub`` / ``friendly`` / ``store`` as each
test needs) so the pure-ish glue runs headless — no Qt stack, no real dialog. The extra ``aqt``
symbols the controllers import at module load are stubbed here before importing them.
"""

from __future__ import annotations

import sys
import types
from typing import Any

# --- stub the extra aqt symbols the dialogs package imports at module load ----------------
# Importing any ``dialogs.controllers.*`` submodule first runs the ``dialogs`` package
# __init__, which loads studio.py + prompt.py (and web_dialog.py) — so stub the
# Qt symbols all of those import at module top.
_theme_mod = types.ModuleType("aqt.theme")
_theme_mod.theme_manager = types.SimpleNamespace(night_mode=False)
sys.modules.setdefault("aqt.theme", _theme_mod)

_qt = sys.modules.get("aqt.qt") or types.ModuleType("aqt.qt")
for _name in (
    "QCloseEvent",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QLabel",
    "QPlainTextEdit",
    "QPushButton",
    "Qt",
    "QVBoxLayout",
    "QWebEngineView",
    "QWidget",
):
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

from omnia.gui.smart_notes.dialogs.controllers.account import (  # noqa: E402
    AccountController,
)
from omnia.gui.smart_notes.dialogs.controllers.authoring import (  # noqa: E402
    AuthoringController,
)
from omnia.gui.smart_notes.dialogs.controllers.config import (  # noqa: E402
    ConfigController,
)
from omnia.gui.smart_notes.dialogs.controllers.graph import (  # noqa: E402
    GraphController,
    _DepPlan,
)
from omnia.gui.smart_notes.dialogs.controllers.native_runtime import (  # noqa: E402
    NativeRuntimeController,
)


def _fake_ctx(**overrides: Any) -> types.SimpleNamespace:
    """A minimal stand-in for ``SmartNotesContext`` (only the helpers a test touches).

    ``friendly`` mirrors the real generic-message branch so error-path assertions stay honest;
    ``eval_js`` / ``build_hub`` / ``store`` default to inert stubs and are overridden per test.
    """
    ctx = types.SimpleNamespace(
        eval_js=lambda js: None,
        build_hub=lambda: None,
        friendly=lambda exc, prefix: f"{prefix} — see logs.",
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _graph(**overrides: Any) -> GraphController:
    """A graph controller over a fake context with an empty deps memo."""
    return GraphController(_fake_ctx(**overrides))


class TestOpRegistryCompleteness:
    """Lock the decomposition invariant: the 5 controllers together register EXACTLY the op set
    the page calls, with no op dropped, duplicated, or accidentally re-registered across two
    controllers. Guards future drift when handlers move between controllers."""

    _EXPECTED_OPS = {
        "list_note_types",
        "load",
        "set_base_field",
        "create_field",
        "save",
        "cancel",
        "install_integration",
        "refresh_install_status",
        "graph_recompute",
        "classify_deps",
        "validate_prompt",
        "rewrite_edges",
        "improve_prompt_pinned",
        "auto_smart",
        "improve_prompt",
        "improve_all",
        "preview",
        "account_data",
        "account_credit",
        "account_test",
        "account_keys",
        "account_keys_credit",
        "set_default_model",
        "set_auto_voice",
        "refresh_voices",
        "set_secrets",
        "browse_file",
        "open_url",
        "replay_audio",
        "native_runtimes",
        "set_native_runtime",
    }

    def test_controllers_cover_every_op_exactly_once(self):
        ctx = _fake_ctx()
        graph = GraphController(ctx)
        controllers = [
            ConfigController(ctx, reject=lambda: None),
            graph,
            AuthoringController(ctx, graph),
            AccountController(ctx),
            NativeRuntimeController(ctx),
        ]
        keys: list[str] = []
        for controller in controllers:
            keys.extend(controller.ops().keys())
        assert len(keys) == len(set(keys)), "an op is registered by two controllers"
        assert set(keys) == self._EXPECTED_OPS


def _row(field: str, prompt: str, depends_on=None) -> dict:
    return {"field": field, "prompt": prompt, "depends_on": depends_on or []}


class TestPlanDepClassification:
    def test_all_refs_become_uncached_items(self):
        ctrl = _graph()
        rows = [
            _row(
                "Example",
                "Use {{Kanji}} and {{Reading}}.",
                [{"field": "Reading", "kind": "soft", "auto": False}],
            )
        ]
        plan = ctrl._plan_dep_classification("Vocab", "Kanji", rows)
        # ALL refs are (re)classified — the prompt is the source of truth for hard/soft, so an
        # existing edge (Reading) is re-read too, not only genuinely-new refs.
        assert plan.uncached_items == [
            ("Example", "Use {{Kanji}} and {{Reading}}.", ["Kanji", "Reading"])
        ]
        assert plan.cached == {}

    def test_row_with_no_new_refs_is_omitted(self):
        ctrl = _graph()
        rows = [_row("Example", "Plain prompt, no refs.")]
        plan = ctrl._plan_dep_classification("Vocab", "Kanji", rows)
        assert plan == _DepPlan()

    def test_memo_hit_skips_the_llm_item(self):
        ctrl = _graph()
        # Pre-seed the memo for this exact (field, prompt) — a prior classify this session.
        ctrl._deps_memo[("Vocab", "Kanji", "Example", "Use {{Kanji}}.")] = {
            "Kanji": "hard"
        }
        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = ctrl._plan_dep_classification("Vocab", "Kanji", rows)
        assert plan.uncached_items == []  # no LLM call needed
        assert plan.cached == {"Example": {"Kanji": "hard"}}


class TestReconcileRows:
    def test_fresh_classification_adds_auto_edges(self):
        ctrl = _graph()
        from omnia.plugins.smart_notes.authoring import EdgeKinding

        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = _DepPlan(
            uncached_items=[("Example", "Use {{Kanji}}.", ["Kanji"])], cached={}
        )
        classified = {"Example": (EdgeKinding(field="Kanji", kind="soft"),)}
        items = ctrl._reconcile_rows("Vocab", "Kanji", rows, plan, classified)
        assert items == [
            {
                "field": "Example",
                "depends_on": [{"field": "Kanji", "kind": "soft", "auto": True}],
            }
        ]
        # The fresh verdict is memoised by (field, prompt) for a later re-save.
        assert ctrl._deps_memo[("Vocab", "Kanji", "Example", "Use {{Kanji}}.")] == {
            "Kanji": "soft"
        }

    def test_memo_cached_verdicts_are_used_without_classification(self):
        ctrl = _graph()
        rows = [_row("Example", "Use {{Kanji}}.")]
        plan = _DepPlan(uncached_items=[], cached={"Example": {"Kanji": "hard"}})
        items = ctrl._reconcile_rows("Vocab", "Kanji", rows, plan, {})
        assert items == [
            {
                "field": "Example",
                "depends_on": [{"field": "Kanji", "kind": "hard", "auto": True}],
            }
        ]

    def test_existing_edge_is_recoloured_to_classification(self):
        # The prompt is the source of truth: reconcile RE-COLOURS Reading soft→hard per the fresh
        # classification (its auto=False existence is preserved; only the kind changes).
        ctrl = _graph()
        from omnia.plugins.smart_notes.authoring import EdgeKinding

        rows = [
            _row(
                "Example",
                "Use {{Kanji}} and {{Reading}}.",
                [{"field": "Reading", "kind": "soft", "auto": False}],
            )
        ]
        plan = _DepPlan(
            uncached_items=[("Example", rows[0]["prompt"], ["Kanji", "Reading"])],
            cached={},
        )
        classified = {
            "Example": (
                EdgeKinding(field="Kanji", kind="hard"),
                EdgeKinding(field="Reading", kind="hard"),
            )
        }
        items = ctrl._reconcile_rows("Vocab", "Kanji", rows, plan, classified)
        deps = {d["field"]: d for d in items[0]["depends_on"]}
        assert deps["Reading"] == {"field": "Reading", "kind": "hard", "auto": False}
        assert deps["Kanji"] == {"field": "Kanji", "kind": "hard", "auto": True}

    def test_vanished_auto_edge_is_dropped_with_no_classification(self):
        ctrl = _graph()
        rows = [
            _row(
                "Example",
                "No references anymore.",
                [{"field": "Kanji", "kind": "hard", "auto": True}],
            )
        ]
        # No new refs → no classify; reconcile still drops the stale auto edge.
        plan = _DepPlan()
        items = ctrl._reconcile_rows("Vocab", "Kanji", rows, plan, {})
        assert items == [{"field": "Example", "depends_on": []}]


class TestValidatePromptBoundary:
    """The graph→prompt popover's live guard rail (``on_validate_prompt``): SYNCHRONOUS, no LLM.
    A candidate must derive EXACTLY the full intended dependency edge set at the node.
    """

    def _validate(self, monkeypatch, *, prompt, intended, known):
        from omnia.core import anki_compat

        monkeypatch.setattr(
            anki_compat, "note_type_field_names", lambda nt: list(known)
        )
        ctrl = _graph()
        return ctrl.on_validate_prompt(
            {
                "note_type": "Vocab",
                "base_field": "Word",
                "target_field": "Definition",
                "prompt": prompt,
                "intended_depends_on": intended,
            }
        )

    def test_clean_reword_same_refs_is_ok(self, monkeypatch):
        res = self._validate(
            monkeypatch,
            prompt="Write the definition of {{Word}} in one line.",
            intended=[{"field": "Word", "kind": "hard"}],
            known=["Word", "Definition", "Note"],
        )
        assert res["consistency"]["ok"] is True
        assert res["consistency"]["added_fields"] == []
        assert res["consistency"]["removed_fields"] == []

    def test_extra_ref_fails_with_added_field_message(self, monkeypatch):
        res = self._validate(
            monkeypatch,
            prompt="Define {{Word}} using {{Note}}.",  # Note is not in the intended set
            intended=[{"field": "Word", "kind": "hard"}],
            known=["Word", "Definition", "Note"],
        )
        assert res["consistency"]["ok"] is False
        assert "note" in res["consistency"]["added_fields"]
        assert any("note" in m.lower() for m in res["consistency"]["messages"])

    def test_dropped_ref_fails_with_removed_field_message(self, monkeypatch):
        res = self._validate(
            monkeypatch,
            prompt="A definition.",  # dropped {{Word}}
            intended=[{"field": "Word", "kind": "hard"}],
            known=["Word", "Definition", "Note"],
        )
        assert res["consistency"]["ok"] is False
        assert "word" in res["consistency"]["removed_fields"]

    def test_brace_syntax_error_is_reported(self, monkeypatch):
        res = self._validate(
            monkeypatch,
            prompt="Define {{Word",  # unclosed
            intended=[{"field": "Word", "kind": "hard"}],
            known=["Word", "Definition"],
        )
        assert res["syntax_errors"]
        assert res["consistency"]["ok"] is False


class TestRewriteEdgesThreadRouting:
    """Mirror of the classify thread-routing guard: the rewrite push must happen ONLY in the
    run_in_background success callback (main thread), never inside the worker (off-main eval_js
    is a native Qt segfault)."""

    def test_push_is_in_the_success_callback_not_the_worker(self, monkeypatch):
        from conftest import FakeLLMProvider

        from omnia.core import anki_compat

        monkeypatch.setattr(
            anki_compat,
            "note_type_field_names",
            lambda nt: ["Word", "Definition", "Reading"],
        )
        captured: dict = {}

        def fake_run(op, *, on_success, on_failure=None, parent=None, label=None):
            captured["op"] = op
            captured["on_success"] = on_success

        monkeypatch.setattr(anki_compat, "run_in_background", fake_run)

        evals: list[str] = []
        ctrl = _graph(
            eval_js=lambda js: evals.append(js),
            build_hub=lambda: types.SimpleNamespace(
                llm=lambda: FakeLLMProvider(text="Define {{Word}} clearly.")
            ),
        )

        ctrl.on_rewrite_edges(
            {
                "note_type": "Vocab",
                "base_field": "Word",
                "changes": [
                    {
                        "target": "Definition",
                        "old_prompt": "Define {{Word}}.",
                        "kept_deps": [],
                        "change": {
                            "action": "toggle",
                            "src": "Word",
                            "new_kind": "soft",
                        },
                        "intended_depends_on": [{"field": "Word", "kind": "soft"}],
                    }
                ],
            }
        )
        # The worker computes off-thread — it must NOT touch the WebView.
        result = captured["op"]()
        assert evals == []
        # The success callback (main thread) is the only place that pushes.
        captured["on_success"](result)
        assert any("__snRewriteResult" in js for js in evals)
        assert result and result[0]["field"] == "Definition"


class TestImprovePinnedThreadRouting:
    def test_push_is_in_the_success_callback_not_the_worker(self, monkeypatch):
        from conftest import FakeLLMProvider

        from omnia.core import anki_compat

        monkeypatch.setattr(
            anki_compat, "note_type_field_names", lambda nt: ["Word", "Definition"]
        )
        captured: dict = {}

        def fake_run(op, *, on_success, on_failure=None, parent=None, label=None):
            captured["op"] = op
            captured["on_success"] = on_success

        monkeypatch.setattr(anki_compat, "run_in_background", fake_run)

        evals: list[str] = []
        ctrl = _graph(
            eval_js=lambda js: evals.append(js),
            build_hub=lambda: types.SimpleNamespace(
                llm=lambda: FakeLLMProvider(text="Define {{Word}} concisely.")
            ),
        )

        ctrl.on_improve_prompt_pinned(
            {
                "note_type": "Vocab",
                "base_field": "Word",
                "target_field": "Definition",
                "prompt": "Define {{Word}}.",
                "fixed_deps": [{"field": "Word", "kind": "hard"}],
            }
        )
        result = captured["op"]()
        assert evals == []
        captured["on_success"](result)
        # W1: the pinned improve pushes to the DEDICATED popover hook, NEVER the prompt editor's
        # shared __snImproveResult (so a stale result can't fall through and write a row).
        assert any("__snDiffImproveResult" in js for js in evals)
        assert not any(
            "__snImproveResult(" in js and "__snDiffImproveResult" not in js
            for js in evals
        )


class TestSaveCycleGuard:
    """The save-path persistence backstop (W2): a cyclic config is refused, not persisted."""

    def _save(self, rows):
        saved: list = []
        store = types.SimpleNamespace(
            load=lambda: types.SimpleNamespace(
                note_types=[],
                generate_at_review=False,
                regenerate_when_batching=True,
                allow_empty_fields=False,
                auto_generate_integrations={},
                copy=lambda update: saved.append(update),
            ),
            save=lambda settings: saved.append(settings),
        )
        ctrl = ConfigController(_fake_ctx(store=store), reject=lambda: None)
        result = ctrl.on_save(
            {"note_type": "Vocab", "base_field": "Word", "rows": rows, "decks": []}
        )
        return result, saved

    def test_cyclic_config_is_rejected_and_not_persisted(self):
        rows = [
            _row("A", "", [{"field": "B", "kind": "hard"}]),
            _row("B", "", [{"field": "A", "kind": "hard"}]),
        ]
        for r in rows:
            r["enabled"] = True
        result, saved = self._save(rows)
        assert "cycle" in (result.get("error") or "").lower()
        assert saved == []  # nothing written

    def test_acyclic_config_saves(self):
        rows = [_row("Definition", "Define {{Word}}")]
        rows[0]["enabled"] = True
        result, _ = self._save(rows)
        assert result == {"ok": True}


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

        evals: list[str] = []
        ctrl = _graph(
            eval_js=lambda js: evals.append(js),
            build_hub=lambda: types.SimpleNamespace(
                llm=lambda: FakeLLMProvider(
                    text='{"Example": [{"field": "Kanji", "kind": "hard"}]}'
                )
            ),
        )

        ctrl.on_classify_deps(
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
