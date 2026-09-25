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

import types
from typing import Any

# --- stub the extra aqt symbols the dialogs package imports at module load ----------------
# Importing any ``dialogs.controllers.*`` submodule first runs the ``dialogs`` package
# __init__, which loads studio.py + prompt.py (and web_dialog.py) — so the Qt symbols all of
# those import at module top have to be in place before the import below runs.
from aqt_stubs import install_gui_stubs

install_gui_stubs()

from conftest import FakeLLMProvider as _FakeLLMProvider  # noqa: E402

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
from omnia.plugins.smart_notes.config import (  # noqa: E402
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
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
        "launch_integration",
        "refresh_install_status",
        "configure_lookup",
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
        "add_endpoint",
        "remove_endpoint",
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

    def _save(self, rows, stored: SmartNotesSettings | None = None, options=None):
        """Run ``on_save`` over a fake store holding ``stored``; return (result, saved)."""
        saved: list[SmartNotesSettings] = []
        settings = SmartNotesSettings() if stored is None else stored
        store = types.SimpleNamespace(
            load=lambda: settings, save=lambda updated: saved.append(updated)
        )
        ctrl = ConfigController(_fake_ctx(store=store), reject=lambda: None)
        payload = {
            "note_type": "Vocab",
            "base_field": "Word",
            "rows": rows,
            "decks": [],
        }
        if options is not None:
            payload["options"] = options
        result = ctrl.on_save(payload)
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

    def test_a_stored_tool_chain_survives_a_save_from_this_page(self):
        # A payload that carries NO ``tools`` key never rendered a chain (an older page, a
        # partial payload) — saving from it must NOT be how a chain configured elsewhere gets
        # deleted. (A page that DOES render the picker posts the key, so clearing still works.)
        stored = SmartNotesSettings(
            note_types=[
                SmartNotesNoteTypeConfig(
                    note_type="Vocab",
                    base_field="Word",
                    fields=[
                        SmartNotesFieldConfig(
                            field="Definition",
                            enabled=True,
                            prompt="Define {{Word}}",
                            tools=[
                                FieldToolConfig(tool="cloze"),
                                FieldToolConfig(tool="ai"),
                            ],
                        )
                    ],
                )
            ]
        )
        rows = [_row("Definition", "Define {{Word}}")]
        rows[0]["enabled"] = True

        result, saved = self._save(rows, stored)

        assert result == {"ok": True}
        persisted = saved[0].note_type_config("Vocab")
        assert persisted is not None
        assert [t.tool for t in persisted.fields[0].tools] == ["cloze", "ai"]


class TestConcurrencyOptionSaveCycle:
    """The Advanced pane's worker count must survive a save that does not mention it."""

    _save = TestSaveCycleGuard._save

    def _rows(self):
        rows = [_row("Definition", "Define {{Word}}")]
        rows[0]["enabled"] = True
        return rows

    def test_a_payload_that_omits_the_option_does_not_reset_it(self):
        # An older page — or one where seeding failed — sends no key. Treating that as "the
        # user chose the default" is how a value set on another device silently disappears.
        stored = SmartNotesSettings(max_concurrent_generations=7)

        _result, saved = self._save(self._rows(), stored, options={})

        assert saved[0].max_concurrent_generations == 7

    def test_a_sent_value_is_persisted(self):
        _result, saved = self._save(
            self._rows(), None, options={"max_concurrent_generations": 5}
        )

        assert saved[0].max_concurrent_generations == 5

    def test_a_nonsense_value_falls_back_to_the_stored_one(self):
        # JavaScript will happily post Infinity or null for a number input; an unbounded worker
        # count is an unbounded fan-out at the provider.
        stored = SmartNotesSettings(max_concurrent_generations=4)

        _result, saved = self._save(
            self._rows(), stored, options={"max_concurrent_generations": None}
        )

        assert saved[0].max_concurrent_generations == 4

    def test_an_out_of_range_value_is_clamped_not_rejected(self):
        _result, saved = self._save(
            self._rows(), None, options={"max_concurrent_generations": 9999}
        )

        assert saved[0].max_concurrent_generations == 16

    def test_a_payload_that_omits_the_batch_size_does_not_reset_it(self):
        # The stakes are higher for this one than for the worker count: its default is 1, i.e.
        # OFF, so treating an absent key as "the default" would silently switch off grouping the
        # user turned on somewhere else.
        stored = SmartNotesSettings(batch_notes_per_call=8)

        _result, saved = self._save(self._rows(), stored, options={})

        assert saved[0].batch_notes_per_call == 8

    def test_a_sent_batch_size_is_persisted(self):
        _result, saved = self._save(
            self._rows(), None, options={"batch_notes_per_call": 6}
        )

        assert saved[0].batch_notes_per_call == 6

    def test_an_out_of_range_batch_size_is_clamped_not_rejected(self):
        _result, saved = self._save(
            self._rows(), None, options={"batch_notes_per_call": 9999}
        )

        assert saved[0].batch_notes_per_call == 20

    def test_an_infinite_batch_size_falls_back_instead_of_clamping(self):
        # ADR-011's post-mortem: Number("1e999") is Infinity, and a number input will post it.
        # There is no honest clamp for "not a number" — a wildly out-of-range VALUE is a slider
        # pushed too far, but Infinity is a broken payload, so the stored value stands.
        stored = SmartNotesSettings(batch_notes_per_call=5)

        _result, saved = self._save(
            self._rows(), stored, options={"batch_notes_per_call": float("inf")}
        )

        assert saved[0].batch_notes_per_call == 5

    def test_saving_an_untouched_pane_writes_no_performance_key_at_all(self):
        """Opening the dialog and pressing Save must not start writing these two keys.

        The pane always posts both numbers when its controls render, so the controller cannot
        tell "the user chose 8" from "the seeded 8 came back unchanged" by presence alone — it
        compares against the STORED value. Naming a key in ``copy(update=…)`` marks it set, and
        :meth:`SmartNotesSettings.dict` serializes exactly the keys that are set, so including an
        unchanged value would make every save write two keys a pre-ADR-010 device rejects with a
        crash on every note-add hook.
        """
        defaults = {
            "max_concurrent_generations": SmartNotesSettings.__fields__[
                "max_concurrent_generations"
            ].default,
            "batch_notes_per_call": SmartNotesSettings.__fields__[
                "batch_notes_per_call"
            ].default,
        }

        _result, saved = self._save(self._rows(), None, options=dict(defaults))

        blob = saved[0].dict()
        assert "max_concurrent_generations" not in blob
        assert "batch_notes_per_call" not in blob

    def test_choosing_this_build_s_default_on_purpose_is_written(self):
        """The other half: a real change TO the default value must persist.

        The old prune dropped any value equal to the current default, so the day the default
        moved to 8 a user who deliberately picked 8 wrote nothing and every other device kept
        running its own default. Changing 4 -> 8 is a change, and it is stored as one.
        """
        default = SmartNotesSettings.__fields__["max_concurrent_generations"].default
        stored = SmartNotesSettings(max_concurrent_generations=4)

        _result, saved = self._save(
            self._rows(), stored, options={"max_concurrent_generations": default}
        )

        assert saved[0].dict()["max_concurrent_generations"] == default


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


class TestThePreviewNamesOnlyWhatWasRead:
    """The panel lists INPUTS, and a dependency is not an input.

    ``rule_prerequisites`` unions what a rule reads with its explicit ``depends_on`` — ordering
    edges: an ``auto`` classifier edge left behind by a prompt the chain stopped reading, a
    hand-drawn SOFT edge that by definition only orders. Listing those names a field the run
    never opened, with a sample value attached, which reads as "this is what I ran against".
    Wide in a way that looks right, which is why it needs a test that says no.
    """

    def _rule(self, **kw):
        from omnia.plugins.smart_notes.config import (
            CompiledToolSpec,
            SmartNotesFieldRule,
        )

        base = {
            "note_type": "Vocab",
            "base_field": "Word",
            "target_field": "Audio",
            "kind": "tts",
            "tools": (
                CompiledToolSpec(name="cloze", params={"sentence_field": "Backup"}),
            ),
        }
        base.update(kw)
        return SmartNotesFieldRule(**base)

    def _shown(self, rule, fields=None):
        from omnia.gui.smart_notes.dialogs.controllers.authoring import (
            AuthoringController,
        )

        controller = AuthoringController.__new__(AuthoringController)
        return [
            entry["field"]
            for entry in controller._preview_inputs(rule, fields or {"WORD": "cat"})
        ]

    def test_a_leftover_prompt_ref_is_not_listed(self):
        """The motivating case, all the way through.

        A field with a stale ``{{WORD}}`` prompt and a Clone Field tool: the prompt text is
        still there, so the classifier keeps a ``FieldDep(WORD, auto=True)`` on the rule. The
        chain never reads the prompt, so WORD is not an input — it is only an edge.
        """
        from omnia.plugins.smart_notes.config import FieldDep

        rule = self._rule(
            prompt="say {{WORD}}",
            depends_on=[FieldDep(field="WORD", kind="hard", auto=True)],
        )

        assert self._shown(rule) == [
            "Backup"
        ], "the preview named the prompt the chain never read, beside the field it did"

    def test_a_hand_drawn_soft_edge_is_not_listed(self):
        # Soft means "order me after this", never "read this". The general form of the above.
        from omnia.plugins.smart_notes.config import FieldDep

        rule = self._rule(
            depends_on=[FieldDep(field="Meaning", kind="soft", auto=False)]
        )

        assert self._shown(rule) == ["Backup"]

    def test_a_prompt_the_chain_DOES_read_is_listed(self):
        # The negative must not overshoot: an `ai` tool's inputs ARE its prompt refs.
        from omnia.plugins.smart_notes.config import CompiledToolSpec, FieldDep

        rule = self._rule(
            prompt="define {{WORD}}",
            tools=(CompiledToolSpec(name="ai"),),
            depends_on=[FieldDep(field="WORD", kind="hard", auto=True)],
        )

        assert self._shown(rule) == ["WORD"]


class TestThePreviewSaysWhatItActuallyRead:
    """The inputs a preview reports must be the inputs it used.

    `rule_source_fields` answers only the prompt's `{{refs}}` and the rule's `source_field`;
    ordering, blocking and the graph all use `rule_prerequisites`, which UNIONs those with the
    fields a TOOL's params name. The preview used the narrow one — so a row whose chain reads
    `Example 1 (audio) (backup)` reported `{{WORD}}`, the prompt that chain never touches, and
    described a run that did not happen.
    """

    def _inputs(self, rule, fields):
        from omnia.gui.smart_notes.dialogs.controllers.authoring import (
            AuthoringController,
        )

        controller = AuthoringController.__new__(AuthoringController)
        return controller._preview_inputs(rule, fields)

    def _rule(self, **kw):
        from omnia.plugins.smart_notes.config import (
            SmartNotesFieldConfig,
        )
        from omnia.plugins.smart_notes.engine import compile_field_rule

        row = SmartNotesFieldConfig(
            field="Audio", enabled=True, type="text", tools=[], **kw
        )
        return compile_field_rule(row, "Word")

    def test_a_field_a_tool_param_names_is_shown(self, monkeypatch):
        """The case that was wrong: the tool's input, not the prompt's."""
        from omnia.plugins.smart_notes.config import CompiledToolSpec

        rule = self._rule(prompt="say {{Word}}").copy(
            update={
                "tools": (
                    CompiledToolSpec(
                        name="cloze", params={"sentence_field": "Sentence"}
                    ),
                )
            }
        )

        shown = [row["field"] for row in self._inputs(rule, {"Sentence": "a sentence"})]

        assert "Sentence" in shown

    def test_its_sample_value_comes_along(self):
        from omnia.plugins.smart_notes.config import CompiledToolSpec

        rule = self._rule(prompt="say {{Word}}").copy(
            update={
                "tools": (
                    CompiledToolSpec(
                        name="cloze", params={"sentence_field": "Sentence"}
                    ),
                )
            }
        )

        shown = {
            row["field"]: row["value"]
            for row in self._inputs(rule, {"Sentence": "a sentence"})
        }

        assert shown["Sentence"] == "a sentence"

    def test_a_promptless_field_still_reports_its_source(self):
        # The one case where what generation READS is wider than what it DEPENDS on: the base
        # field is always present, so it is not an edge — but the run does read it, and a
        # preview claiming to have run on nothing is worse than a redundant row.
        rule = self._rule()

        shown = [row["field"] for row in self._inputs(rule, {"Word": "survive"})]

        assert shown == ["Word"]


class TestPreviewRunsTheRowsToolChain:
    """Sync point 5: ▶ Preview must run the row's OWN chain, not always the AI path.

    The preview builds its rule straight from the posted row, so before the picker landed it
    inherited the default ``("ai",)`` chain. A row configured "cloze → ai" would then preview an
    LLM call the real run never makes — wrong output AND a bill for it.
    """

    class _CountingLLM(_FakeLLMProvider):
        """A real ``LLMProvider`` subclass — the preview drives the whole interface."""

        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def generate_text(
            self, prompt, *, system=None, temperature=0.7, max_tokens=None
        ):
            self.prompts.append(prompt)
            return f"llm:{prompt}"

    class _Note:
        def __init__(self, fields: dict[str, str]) -> None:
            self._fields = fields

        def keys(self):
            return list(self._fields)

        def __getitem__(self, key: str) -> str:
            return self._fields[key]

    def _preview(self, monkeypatch, row: dict) -> tuple[list[str], list[str]]:
        """Run ``on_preview`` inline and return (pushed js, LLM prompts)."""
        from omnia.core import anki_compat

        llm = self._CountingLLM()
        monkeypatch.setattr(
            anki_compat,
            "random_note_of_type",
            lambda nt: self._Note(
                {"Word": "survive", "Sentence": "They survived.", "Cloze": ""}
            ),
        )
        captured: dict[str, Any] = {}

        def fake_run(op, *, on_success, on_failure=None, parent=None, label=None):
            captured["result"] = op()
            on_success(captured["result"])

        monkeypatch.setattr(anki_compat, "run_in_background", fake_run)
        evals: list[str] = []
        ctx = _fake_ctx(
            eval_js=lambda js: evals.append(js),
            build_hub=lambda: types.SimpleNamespace(
                llm=lambda **kwargs: llm, tts=lambda **kwargs: None
            ),
            result_payload=lambda result: {"kind": result.kind, "text": result.text},
        )
        AuthoringController(ctx, GraphController(ctx)).on_preview(row)
        return evals, llm.prompts

    def _row(self, **kw) -> dict:
        row = {
            "note_type": "Vocab",
            "base_field": "Word",
            "field": "Cloze",
            "type": "text",
            "prompt": "Cloze {{Word}} in {{Sentence}}",
            "provider": "",
            "model": "",
            "voice": "",
            "language": "",
            "tools": [],
        }
        row.update(kw)
        return row

    def test_a_cloze_row_previews_the_cloze_never_the_llm(self, monkeypatch):
        evals, prompts = self._preview(
            monkeypatch,
            self._row(
                tools=[
                    {"tool": "cloze", "params": {"sentence_field": "Sentence"}},
                    {"tool": "ai", "params": {}},
                ]
            ),
        )

        assert prompts == []  # the LLM was never asked
        # The mask, not Anki cloze markup: eight letters, first one shown.
        assert "They s_______." in evals[0]

    def test_a_row_the_page_posted_with_no_chain_previews_nothing(self, monkeypatch):
        """Unticking every tool must not reach a provider.

        It used to: an empty chain compiled to `ai`, so Preview generated — and billed — with a
        tool the row does not list, and the only hint was a chip in a column. The preview says
        why instead of guessing.
        """
        evals, prompts = self._preview(monkeypatch, self._row())

        assert prompts == [], "it called the provider for a row with no tool"
        assert "No tool is configured" in evals[0]


class TestAddingAnEndpointFromThePage:
    """The Keys subtab can create and delete the user's own endpoints.

    The reply to both carries the refreshed card list, so the change is on screen as part of the
    click that made it. Refetching instead leaves a window in which the page and the config
    disagree about what exists — and the thing being added is a credential store, which is a
    poor subject for a window of disagreement.
    """

    def _controller(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        ctx = _fake_ctx()
        ctx.repo = ConfigRepository(ConfigLoader(config_dir))
        return AccountController(ctx)

    def test_adding_one_returns_the_refreshed_cards(self, config_dir):
        controller = self._controller(config_dir)

        result = controller.on_add_endpoint({"label": "gpu-box"})

        assert result["provider"] == "custom:gpu-box"
        ids = [c["id"] for c in result["providers"]]
        assert "custom:gpu-box" in ids, "the new card was not in the same reply"

    def test_the_new_card_is_marked_removable(self, config_dir):
        controller = self._controller(config_dir)

        cards = controller.on_add_endpoint({"label": "gpu-box"})["providers"]
        card = next(c for c in cards if c["id"] == "custom:gpu-box")

        assert card["custom"] is True

    def test_a_shipped_card_is_not_removable(self, config_dir):
        controller = self._controller(config_dir)

        cards = controller.on_add_endpoint({"label": "gpu-box"})["providers"]
        card = next(c for c in cards if c["id"] == "gemini")

        assert card["custom"] is False

    def test_a_duplicate_name_comes_back_as_a_message_not_a_crash(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "mine"})

        result = controller.on_add_endpoint({"label": "mine"})

        assert "already" in result["error"]
        assert "providers" not in result, "a failed add must not redraw the list"

    def test_a_blank_name_is_refused(self, config_dir):
        controller = self._controller(config_dir)

        assert controller.on_add_endpoint({"label": "   "})["error"]

    def test_removing_one_returns_the_refreshed_cards(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gone"})

        result = controller.on_remove_endpoint({"provider": "custom:gone"})

        assert "custom:gone" not in [c["id"] for c in result["providers"]]

    def test_removing_a_shipped_provider_is_refused(self, config_dir):
        controller = self._controller(config_dir)

        result = controller.on_remove_endpoint({"provider": "gemini"})

        assert result["error"]


class TestTheKeysPageOffersTheAddRow:
    """The JS half: rendering the cards must also render the way to make one."""

    def _js(self) -> str:
        import omnia.gui.smart_notes.html as html_module
        from omnia.gui.assets import read_asset

        return read_asset(html_module.__file__, "web", "05-handlers.js")

    def test_the_add_row_is_rendered_with_the_cards(self):
        js = self._js()
        render = js[js.index("function renderKeys") :]
        render = render[: render.index("\n  }")]

        assert "addEndpointRow()" in render

    def test_it_sends_the_add_op(self):
        assert 'send("add_endpoint"' in self._js()

    def test_removing_is_confirmed_first(self):
        """It deletes the stored key too, and there is no undo."""
        js = self._js()
        assert "window.confirm" in js
        assert 'send("remove_endpoint"' in js

    def test_only_a_custom_card_gets_a_remove_button(self):
        js = self._js()
        card = js[js.index("function keyCard") :]

        assert "if (card.custom)" in card


class TestAnEndpointChangeRefreshesEverythingItInvalidates:
    """Adding or removing an endpoint changes three things on screen, not one.

    The reply carried only the Keys cards. The Account default picker is built from a provider
    list baked into the catalog when the dialog opened, so a removed endpoint stayed selectable
    there — and choosing a model for it wrote its section back, resurrecting in Keys an
    endpoint whose stored key had already been shredded. A newly added one, conversely, could
    not be chosen as a default until the dialog was reopened.
    """

    def _controller(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        ctx = _fake_ctx()
        ctx.repo = ConfigRepository(ConfigLoader(config_dir))
        return AccountController(ctx)

    def test_a_new_endpoint_is_immediately_selectable_as_a_default(self, config_dir):
        controller = self._controller(config_dir)

        res = controller.on_add_endpoint({"label": "gpu-box"})

        assert "custom:gpu-box" in res["llm_providers"]

    def test_a_new_endpoint_is_immediately_selectable_for_an_image_field(
        self, config_dir
    ):
        """The image picker reads its OWN list — a narrower one — so refreshing three of the
        four keys leaves it holding the list the dialog opened with."""
        controller = self._controller(config_dir)

        res = controller.on_add_endpoint({"label": "gpu-box"})

        assert "custom:gpu-box" in res["image_providers"]

    def test_a_removed_endpoint_leaves_every_picker(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})

        res = controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        assert "custom:gpu-box" not in res["image_providers"]

    def test_a_removed_endpoint_leaves_the_picker(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})

        res = controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        assert "custom:gpu-box" not in res["llm_providers"]

    def test_removing_the_active_one_hands_back_defaults_that_no_longer_name_it(
        self, config_dir
    ):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})
        controller.on_set_default_model(
            {"kind": "text", "provider": "custom:gpu-box", "model": "llama-3.1"}
        )

        res = controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        assert res["defaults"]["text"]["provider"] != "custom:gpu-box"

    def test_choosing_a_model_for_a_removed_endpoint_says_so_instead_of_recreating_it(
        self, config_dir
    ):
        """What a stale picker would send. It must not bring the endpoint back."""
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})
        controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        res = controller.on_set_default_model(
            {"kind": "text", "provider": "custom:gpu-box", "model": "llama-3.1"}
        )

        assert res.get("error")
        assert not any(
            card["id"] == "custom:gpu-box"
            for card in controller.on_account_keys({})["providers"]
        )

    def test_the_keys_cards_still_come_back_too(self, config_dir):
        controller = self._controller(config_dir)

        res = controller.on_add_endpoint({"label": "gpu-box"})

        assert any(card["id"] == "custom:gpu-box" for card in res["providers"])


class TestTheCatalogActuallySeesTheUsersEndpoints:
    """The reader asked the context for a method it does not have.

    `SmartNotesContext` exposes `.repo`; there is no `ctx.llm_settings()`. The call raised
    `AttributeError` into a bare `except Exception` meant for an unparseable config, so it
    returned empty every single time: no custom endpoint ever reached a Provider dropdown, and
    the configured-model merge — the whole reason a self-hosted endpoint's own model id is
    selectable per field — never ran. Nothing failed, which is why nothing showed it.

    Both call sites go through one reader now, so the dialog's bake and the Account panel's
    rebuild cannot answer "which providers exist" differently.
    """

    def _ctx(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        ctx = _fake_ctx()
        ctx.repo = ConfigRepository(ConfigLoader(config_dir))
        return ctx

    def test_an_endpoint_the_user_added_is_in_the_catalog(self, config_dir):
        from omnia.gui.smart_notes.catalog_inputs import CatalogInputs

        ctx = self._ctx(config_dir)
        ctx.repo.add_custom_provider("llm", "gpu")

        assert "custom:gpu" in CatalogInputs.read(ctx).catalog()["llm_providers"]

    def test_its_configured_model_is_offered_for_it(self, config_dir):
        """It has no curated ids, so without this the picker offers nothing at all for it."""
        from omnia.gui.smart_notes.catalog_inputs import CatalogInputs

        ctx = self._ctx(config_dir)
        ctx.repo.add_custom_provider("llm", "gpu")
        ctx.repo.set_active_llm("custom:gpu", text_model="qwen2.5-14b")

        catalog = CatalogInputs.read(ctx).catalog()

        assert "qwen2.5-14b" in catalog["text_models"]["custom:gpu"]

    def test_a_shipped_providers_configured_model_is_folded_in_too(self, config_dir):
        from omnia.gui.smart_notes.catalog_inputs import CatalogInputs

        ctx = self._ctx(config_dir)
        ctx.repo.set_active_llm("openai_compatible", text_model="some-local-build")

        catalog = CatalogInputs.read(ctx).catalog()

        assert "some-local-build" in catalog["text_models"]["openai_compatible"]

    def test_a_context_that_cannot_be_read_still_yields_a_catalog(self, config_dir):
        """A providers.toml that will not parse costs the endpoints, not the dialog."""
        from omnia.gui.smart_notes.catalog_inputs import CatalogInputs

        class Broken:
            @property
            def repo(self):
                raise RuntimeError("providers.toml is not valid TOML")

        inputs = CatalogInputs.read(Broken())

        assert inputs.custom_providers == []
        assert inputs.catalog()["llm_providers"]

    def test_the_rebuild_and_the_bake_agree(self, config_dir):
        """One reader, so the Account reply and the dialog's bake cannot disagree."""
        from omnia.gui.smart_notes.catalog_inputs import CatalogInputs
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        ctx = self._ctx(config_dir)
        res = AccountController(ctx).on_add_endpoint({"label": "gpu"})

        baked = CatalogInputs.read(ctx).catalog()
        assert res["llm_providers"] == baked["llm_providers"]
        assert res["text_models"] == baked["text_models"]


class TestSavingACardForAnEndpointThatIsGone:
    """The Keys list is drawn once; a card can outlive the endpoint it belongs to.

    `_provider_table` now refuses to recreate the section, which is what makes removal stick —
    but the save handler caught broad `Exception` and answered "Could not save — see logs.",
    sending the user to a log file to find out that the thing they were editing no longer
    exists. Add and remove already say which endpoint they mean.
    """

    def _controller(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        ctx = _fake_ctx()
        ctx.repo = ConfigRepository(ConfigLoader(config_dir))
        return AccountController(ctx)

    def test_the_message_names_the_endpoint(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})
        controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        res = controller.on_set_secrets(
            {
                "provider": "custom:gpu-box",
                "fields": [{"key": "api_key", "type": "secret", "value": "sk-1"}],
            }
        )

        assert "gpu-box" in res["error"]
        assert "logs" not in res["error"]

    def test_it_does_not_bring_the_endpoint_back(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})
        controller.on_remove_endpoint({"provider": "custom:gpu-box"})

        controller.on_set_secrets(
            {
                "provider": "custom:gpu-box",
                "fields": [{"key": "api_key", "type": "secret", "value": "sk-1"}],
            }
        )

        assert not any(
            card["id"] == "custom:gpu-box"
            for card in controller.on_account_keys({})["providers"]
        )

    def test_a_live_card_still_saves(self, config_dir):
        controller = self._controller(config_dir)
        controller.on_add_endpoint({"label": "gpu-box"})

        res = controller.on_set_secrets(
            {
                "provider": "custom:gpu-box",
                "fields": [{"key": "api_key", "type": "secret", "value": "sk-1"}],
            }
        )

        assert res.get("ok")
