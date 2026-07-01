"""Tests for the pure Smart Notes HTML builder + row↔config mapping (no Qt/aqt)."""

from __future__ import annotations

import json

from omnia.gui.smart_notes.html import (
    build_smart_notes_html,
    cycle_error_for_config,
    field_configs_from_payload,
    graph_payload,
    load_payload,
    merge_note_type_into,
    note_type_config_from_payload,
    resolve_base_field,
    row_to_payload,
    rows_for_note_type,
)
from omnia.plugins.smart_notes.authoring import AutoSmartField, apply_auto_smart
from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)


def _config(base: str = "Word", fields=None) -> SmartNotesNoteTypeConfig:
    return SmartNotesNoteTypeConfig(
        note_type="Vocab", base_field=base, fields=fields or []
    )


def _row(field: str, **kw) -> dict:
    base = {
        "field": field,
        "enabled": False,
        "type": "text",
        "prompt": "",
        "prompt_locked": False,
        "provider": "",
        "model": "",
        "voice": "",
        "language": "",
        "overwrite": False,
        "depends_on": [],
    }
    base.update(kw)
    return base


class TestGraphPayload:
    def test_nodes_and_derived_edge(self):
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                )
            ],
        )
        gp = graph_payload(cfg)
        names = {n["name"] for n in gp["nodes"]}
        assert {"Word", "Definition"} <= names
        base = next(n for n in gp["nodes"] if n["name"] == "Word")
        assert base["is_base"] is True
        # the {{Word}} reference yields a derived, default-hard edge Word -> Definition
        assert {
            "src": "Word",
            "dst": "Definition",
            "kind": "hard",
            "derived": True,
            "cycle": False,
        } in gp["edges"]
        # every node carries a layout column/row (computed in Python)
        assert all("column" in n and "row" in n for n in gp["nodes"])
        # ...plus pixel geometry for the flow canvas and a top-level bounds.
        assert all(
            all(k in n for k in ("x", "y", "w", "h", "lane")) for n in gp["nodes"]
        )
        assert gp["bounds"]["width"] > 0 and gp["bounds"]["height"] > 0

    def test_explicit_soft_overrides_derived_hard(self):
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                    depends_on=[FieldDep(field="Word", kind="soft")],
                )
            ],
        )
        edge = next(
            e
            for e in graph_payload(cfg)["edges"]
            if e["dst"] == "Definition" and e["src"] == "Word"
        )
        assert edge["kind"] == "soft"

    def test_cyclic_config_renders_with_flag(self):
        # Auto-prompt can write two prompts that reference each other (POS <-> Definition). The
        # graph MUST still lay out — it's how the user sees and breaks the cycle — so graph_payload
        # does not raise, sets has_cycle, and flags exactly the looping edges with cycle=True.
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="POS",
                    enabled=True,
                    type="text",
                    prompt="Part of speech of {{Word}} as a {{Definition}}",
                ),
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}} ({{POS}})",
                ),
            ],
        )
        gp = graph_payload(cfg)  # no raise
        assert gp["has_cycle"] is True
        cyclic = {(e["src"], e["dst"]) for e in gp["edges"] if e["cycle"]}
        assert ("Definition", "POS") in cyclic
        assert ("POS", "Definition") in cyclic
        # the acyclic Word -> POS / Word -> Definition edges are NOT flagged
        assert all(not e["cycle"] for e in gp["edges"] if e["src"] == "Word")
        # every node is still placed (the whole graph renders)
        assert {"Word", "POS", "Definition"} <= {n["name"] for n in gp["nodes"]}
        # these derived refs default to HARD, so it IS a real (blocking) cycle — save is rejected.
        assert cycle_error_for_config(cfg) != ""

    def test_soft_cycle_is_not_flagged_or_blocked(self):
        # POS and Definition SOFTLY reference each other (the classifier labelled the mutual refs
        # soft, as Auto-prompt does). A soft edge is optional metadata the generator can break, so
        # the cycle is generatable: graph_payload must NOT flag it and save must NOT be blocked.
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="POS",
                    enabled=True,
                    type="text",
                    prompt="POS of {{Word}} using {{Definition}}",
                    depends_on=[FieldDep(field="Definition", kind="soft")],
                ),
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}} ({{POS}})",
                    depends_on=[FieldDep(field="POS", kind="soft")],
                ),
            ],
        )
        gp = graph_payload(cfg)
        assert gp["has_cycle"] is False
        assert all(not e["cycle"] for e in gp["edges"])
        kinds = {(e["src"], e["dst"]): e["kind"] for e in gp["edges"]}
        assert kinds.get(("Definition", "POS")) == "soft"
        assert kinds.get(("POS", "Definition")) == "soft"
        # the hard Word -> POS / Word -> Definition edges keep it a valid DAG → save allowed.
        assert cycle_error_for_config(cfg) == ""

    def test_acyclic_config_has_no_cycle_flag(self):
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                )
            ],
        )
        gp = graph_payload(cfg)
        assert gp["has_cycle"] is False
        assert all(e["cycle"] is False for e in gp["edges"])

    def test_depends_on_round_trips_through_payload(self):
        rows = [
            _row(
                "Definition",
                enabled=True,
                type="text",
                depends_on=[{"field": "Word", "kind": "soft"}],
            )
        ]
        cfg = note_type_config_from_payload("Vocab", "Word", rows)
        field = cfg.fields[0]
        assert [(d.field, d.kind) for d in field.depends_on] == [("Word", "soft")]
        # auto defaults to False (a user/explicit edge) and round-trips through the payload.
        assert row_to_payload(field)["depends_on"] == [
            {"field": "Word", "kind": "soft", "auto": False}
        ]

    def test_node_carries_locked_flag_from_field_config(self):
        # A locked field's node reports locked=True; every other node (incl. the base) is False.
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                    prompt_locked=True,
                ),
                SmartNotesFieldConfig(
                    field="Example",
                    enabled=True,
                    type="text",
                    prompt="Use {{Word}}",
                ),
            ],
        )
        by_name = {n["name"]: n for n in graph_payload(cfg)["nodes"]}
        assert by_name["Definition"]["locked"] is True
        assert by_name["Example"]["locked"] is False
        assert by_name["Word"]["locked"] is False

    def test_node_positions_override_layout_and_grow_bounds(self):
        # A pinned position replaces the node's flow x/y, is echoed in node_positions, and grows
        # the canvas bounds to include it (case-insensitive on the field name).
        cfg = _config(
            base="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                )
            ],
        ).copy(update={"node_positions": {"Definition": [500.0, 300.0]}})
        gp = graph_payload(cfg)
        node = next(n for n in gp["nodes"] if n["name"] == "Definition")
        assert node["x"] == 500.0 and node["y"] == 300.0
        assert gp["node_positions"] == {"Definition": [500.0, 300.0]}
        # bounds grow to include the moved node (its right/bottom edge + padding).
        assert gp["bounds"]["width"] >= 500.0 + node["w"]
        assert gp["bounds"]["height"] >= 300.0 + node["h"]

    def test_auto_flag_round_trips_so_classifier_kind_is_not_downgraded(self):
        # A classifier-written edge carries auto=True; the page must preserve it through
        # readDependsOn/collectRows so a re-save keeps it an auto edge (not downgraded to user).
        rows = [
            _row(
                "Definition",
                enabled=True,
                type="text",
                depends_on=[{"field": "Word", "kind": "hard", "auto": True}],
            )
        ]
        cfg = note_type_config_from_payload("Vocab", "Word", rows)
        dep = cfg.fields[0].depends_on[0]
        assert (dep.field, dep.kind, dep.auto) == ("Word", "hard", True)
        assert row_to_payload(cfg.fields[0])["depends_on"] == [
            {"field": "Word", "kind": "hard", "auto": True}
        ]


class TestCycleErrorForConfig:
    """The save-path persistence backstop (W2): reject a config whose deps form a cycle."""

    def test_acyclic_config_has_no_error(self):
        cfg = note_type_config_from_payload(
            "Vocab",
            "Word",
            [
                _row(
                    "Definition",
                    enabled=True,
                    prompt="Define {{Word}}",
                ),
                _row(
                    "Example",
                    enabled=True,
                    prompt="Use {{Word}} in a sentence",
                ),
            ],
        )
        assert cycle_error_for_config(cfg) == ""

    def test_cyclic_config_is_rejected(self):
        # A <-> B via explicit deps forms a cycle the save path must refuse to persist.
        cfg = note_type_config_from_payload(
            "Vocab",
            "Word",
            [
                _row(
                    "A",
                    enabled=True,
                    depends_on=[{"field": "B", "kind": "hard"}],
                ),
                _row(
                    "B",
                    enabled=True,
                    depends_on=[{"field": "A", "kind": "hard"}],
                ),
            ],
        )
        msg = cycle_error_for_config(cfg)
        assert msg and "cycle" in msg.lower()

    def test_self_loop_is_rejected(self):
        cfg = note_type_config_from_payload(
            "Vocab",
            "Word",
            [_row("A", enabled=True, prompt="Use {{A}}")],
        )
        assert cycle_error_for_config(cfg)


class TestRowsForNoteType:
    def test_excludes_base_field(self):
        rows = rows_for_note_type(_config("Word"), ["Word", "Meaning", "IPA"], "Word")
        assert [r.field for r in rows] == ["Meaning", "IPA"]

    def test_new_field_gets_defaults(self):
        rows = rows_for_note_type(_config("Word"), ["Word", "Meaning"], "Word")
        meaning = rows[0]
        assert meaning.field == "Meaning"
        assert meaning.enabled is False
        assert meaning.type == "text"
        assert meaning.prompt == ""

    def test_saved_row_is_preserved(self):
        saved = SmartNotesFieldConfig(
            field="Meaning", enabled=True, prompt="Define {{Word}}", prompt_locked=True
        )
        rows = rows_for_note_type(_config("Word", [saved]), ["Word", "Meaning"], "Word")
        assert rows[0].enabled is True
        assert rows[0].prompt == "Define {{Word}}"
        assert rows[0].prompt_locked is True

    def test_saved_row_for_missing_field_is_dropped(self):
        saved = SmartNotesFieldConfig(field="Gone", enabled=True)
        rows = rows_for_note_type(_config("Word", [saved]), ["Word", "Meaning"], "Word")
        assert [r.field for r in rows] == ["Meaning"]

    def test_order_follows_live_fields(self):
        saved = [
            SmartNotesFieldConfig(field="IPA"),
            SmartNotesFieldConfig(field="Meaning"),
        ]
        rows = rows_for_note_type(
            _config("Word", saved), ["Word", "Meaning", "IPA"], "Word"
        )
        assert [r.field for r in rows] == ["Meaning", "IPA"]

    def test_no_saved_config(self):
        rows = rows_for_note_type(None, ["Word", "Meaning"], "Word")
        assert [r.field for r in rows] == ["Meaning"]

    def test_base_change_excludes_new_base(self):
        rows = rows_for_note_type(
            _config("Word"), ["Word", "Meaning", "IPA"], "Meaning"
        )
        assert [r.field for r in rows] == ["Word", "IPA"]


class TestResolveBaseField:
    def test_saved_base_kept_when_present(self):
        assert resolve_base_field(_config("Word"), ["Word", "Meaning"]) == "Word"

    def test_falls_back_to_first_field_when_saved_missing(self):
        assert resolve_base_field(_config("Gone"), ["Front", "Back"]) == "Front"

    def test_first_field_when_no_config(self):
        assert resolve_base_field(None, ["Front", "Back"]) == "Front"

    def test_empty_when_no_fields(self):
        assert resolve_base_field(None, []) == ""


class TestFieldConfigsFromPayload:
    def test_maps_every_field(self):
        configs = field_configs_from_payload(
            [_row("Meaning", enabled=True, prompt="Define {{Word}}")]
        )
        assert len(configs) == 1
        assert configs[0].field == "Meaning"
        assert configs[0].enabled is True
        assert configs[0].prompt == "Define {{Word}}"

    def test_skips_rows_without_a_field_name(self):
        configs = field_configs_from_payload([_row(""), _row("Meaning")])
        assert [c.field for c in configs] == ["Meaning"]

    def test_invalid_type_falls_back_to_text(self):
        configs = field_configs_from_payload([_row("X", type="bogus")])
        assert configs[0].type == "text"

    def test_preserves_overrides_and_flags(self):
        configs = field_configs_from_payload(
            [
                _row(
                    "Audio",
                    type="tts",
                    provider="gemini",
                    model="gemini-2.0-flash",
                    voice="alloy",
                    overwrite=True,
                    prompt_locked=True,
                )
            ]
        )
        c = configs[0]
        assert (c.type, c.provider, c.model, c.voice) == (
            "tts",
            "gemini",
            "gemini-2.0-flash",
            "alloy",
        )
        assert c.overwrite is True and c.prompt_locked is True


class TestNoteTypeConfigFromPayload:
    def test_assembles_full_config(self):
        config = note_type_config_from_payload(
            "Vocab", "Word", [_row("Meaning", enabled=True)]
        )
        assert config.note_type == "Vocab"
        assert config.base_field == "Word"
        assert [f.field for f in config.fields] == ["Meaning"]

    def test_generatable_excludes_base_and_disabled(self):
        config = note_type_config_from_payload(
            "Vocab",
            "Word",
            [_row("Meaning", enabled=True), _row("IPA", enabled=False)],
        )
        assert [f.field for f in config.generatable_fields()] == ["Meaning"]


class TestRoundTrip:
    def test_payload_to_config_to_payload_is_stable(self):
        rows = [
            _row("Meaning", enabled=True, prompt="Define {{Word}}", overwrite=True),
            _row("Audio", type="tts", provider="gemini"),
        ]
        config = note_type_config_from_payload("Vocab", "Word", rows)
        round_tripped = [row_to_payload(f) for f in config.fields]
        assert round_tripped == rows

    def test_load_payload_round_trips_saved_config(self):
        saved = SmartNotesFieldConfig(
            field="Meaning", enabled=True, prompt="Define {{Word}}"
        )
        payload = load_payload(
            "Vocab", _config("Word", [saved]), ["Word", "Meaning"], ["gemini"]
        )
        rebuilt = note_type_config_from_payload(
            payload["note_type"], payload["base_field"], payload["rows"]
        )
        assert rebuilt.base_field == "Word"
        assert rebuilt.fields[0].prompt == "Define {{Word}}"


class TestLoadPayload:
    def test_includes_providers_and_base(self):
        payload = load_payload(
            "Vocab", _config("Word"), ["Word", "Meaning"], ["gemini", "openai"]
        )
        assert payload["providers"] == ["gemini", "openai"]
        assert payload["base_field"] == "Word"
        assert payload["all_fields"] == ["Word", "Meaning"]
        assert [r["field"] for r in payload["rows"]] == ["Meaning"]

    def test_includes_decks_and_all_decks(self):
        config = _config("Word").copy(update={"decks": [1, 2]})
        payload = load_payload(
            "Vocab",
            config,
            ["Word", "Meaning"],
            ["gemini"],
            all_decks=[{"id": 1, "name": "Default"}],
        )
        assert payload["decks"] == [1, 2]
        assert payload["all_decks"] == [{"id": 1, "name": "Default"}]

    def test_decks_and_all_decks_default_empty(self):
        payload = load_payload("Vocab", None, ["Word", "Meaning"], ["gemini"])
        assert payload["decks"] == []
        assert payload["all_decks"] == []


class TestNoteTypeConfigDecks:
    def test_decks_passed_through(self):
        config = note_type_config_from_payload(
            "Vocab", "Word", [_row("Meaning", enabled=True)], [1, 2]
        )
        assert config.decks == [1, 2]

    def test_decks_default_empty(self):
        config = note_type_config_from_payload(
            "Vocab", "Word", [_row("Meaning", enabled=True)]
        )
        assert config.decks == []


class TestNoteTypeConfigPositions:
    def test_positions_passed_through_as_floats(self):
        config = note_type_config_from_payload(
            "Vocab",
            "Word",
            [_row("Meaning", enabled=True)],
            positions={"X": [1, 2]},
        )
        assert config.node_positions == {"X": [1.0, 2.0]}

    def test_malformed_positions_dropped(self):
        config = note_type_config_from_payload(
            "Vocab",
            "Word",
            [_row("Meaning", enabled=True)],
            positions={
                "Ok": [3, 4],
                "TooShort": [5],
                "TooLong": [6, 7, 8],
                "NotAList": "nope",
            },
        )
        assert config.node_positions == {"Ok": [3.0, 4.0]}

    def test_positions_default_empty(self):
        config = note_type_config_from_payload(
            "Vocab", "Word", [_row("Meaning", enabled=True)]
        )
        assert config.node_positions == {}


class TestMergeNoteTypeInto:
    def test_replaces_same_name(self):
        existing = [
            _config("Word").copy(update={"note_type": "Vocab", "base_field": "A"}),
            _config("Word").copy(update={"note_type": "Other"}),
        ]
        updated = _config("Word").copy(update={"note_type": "Vocab", "base_field": "B"})
        merged = merge_note_type_into(existing, updated)
        by_name = {nt.note_type: nt for nt in merged}
        assert len(merged) == 2
        assert by_name["Vocab"].base_field == "B"
        assert "Other" in by_name

    def test_appends_new_name(self):
        merged = merge_note_type_into([], _config("Word"))
        assert [nt.note_type for nt in merged] == ["Vocab"]


class TestAutoSmartRowRoundTrip:
    def test_auto_smart_fills_only_enabled_unlocked(self):
        # Build a config from posted rows, apply suggestions, and confirm what changed.
        rows = [
            _row("Meaning", enabled=True),
            _row("IPA", enabled=True, prompt_locked=True, prompt="keep me"),
            _row("Notes", enabled=False),
        ]
        config = note_type_config_from_payload("Vocab", "Word", rows)
        suggestions = {
            "Meaning": AutoSmartField(type="text", prompt="Define {{Word}}"),
            "IPA": AutoSmartField(type="text", prompt="overwrite?"),
            "Notes": AutoSmartField(type="text", prompt="ignored"),
        }
        updated = apply_auto_smart(config, suggestions)
        by_field = {f.field: f for f in updated.fields}
        assert by_field["Meaning"].prompt == "Define {{Word}}"  # enabled + unlocked
        assert by_field["IPA"].prompt == "keep me"  # locked -> untouched
        assert by_field["Notes"].prompt == ""  # disabled -> untouched

    def test_updated_config_serializes_back_to_rows(self):
        config = note_type_config_from_payload(
            "Vocab", "Word", [_row("Meaning", enabled=True)]
        )
        updated = apply_auto_smart(
            config, {"Meaning": AutoSmartField(type="tts", prompt="Say {{Word}}")}
        )
        payload = [row_to_payload(f) for f in updated.fields]
        assert payload[0]["type"] == "tts"
        assert payload[0]["prompt"] == "Say {{Word}}"


class TestBuildSmartNotesHtml:
    def test_dark_flag_selects_body_class(self):
        assert "omnia-dark" in build_smart_notes_html(dark=True)
        assert "omnia-light" in build_smart_notes_html(dark=False)

    def test_header_and_persona_copy_present(self):
        html = build_smart_notes_html(dark=False)
        assert "Smart Notes" in html
        assert "senior language master" in html

    def test_ops_wired_in_js(self):
        html = build_smart_notes_html(dark=False)
        for op in (
            "list_note_types",
            "load",
            "set_base_field",
            "create_field",
            "auto_smart",
            "save",
        ):
            assert f'send("{op}"' in html or f'"{op}"' in html

    def test_field_types_injected_for_the_type_dropdown(self):
        html = build_smart_notes_html(dark=False)
        assert json.dumps(("text", "tts", "image")) in html

    def test_table_columns_present(self):
        html = build_smart_notes_html(dark=False)
        for col in ("Field", "Type", "Prompt", "Provider", "Model", "Overwrite"):
            assert col in html

    def test_auto_result_hook_exposed(self):
        # Auto-smart's off-thread result is pushed via this global hook.
        assert "window.__snAutoResult" in build_smart_notes_html(dark=False)

    def test_generate_lock_and_preview_columns_present(self):
        html = build_smart_notes_html(dark=False)
        for col in (">Generate<", ">Lock<", ">Preview<", ">Model<"):
            assert col in html

    def test_catalog_is_baked_with_providers(self):
        html = build_smart_notes_html(dark=False)
        assert "window.__SN_CATALOG" in html
        # The curated provider names land in the baked catalog JSON.
        assert "gemini" in html and "edge_tts" in html

    def test_new_ops_wired_in_js(self):
        html = build_smart_notes_html(dark=False)
        for op in ("improve_prompt", "improve_all", "preview"):
            assert op in html

    def test_voice_column_present_and_no_language_picker(self):
        html = build_smart_notes_html(dark=False)
        # The Voice column stays; the Language picker was removed (a voice fixes the language,
        # else the engine auto-detects). The conditional-column mechanism still gates Voice.
        assert ">Voice<" in html and "sn-col-voice" in html
        assert "sn-has-sound" in html  # the conditional-column mechanism
        assert ">Language<" not in html and "sn-col-language" not in html

    def test_prompt_editor_and_improve_hooks_present(self):
        html = build_smart_notes_html(dark=False)
        assert "sn-modal" in html  # the popup prompt editor
        assert "window.__snImproveResult" in html
        assert "window.__snImproveAllResult" in html
        assert "window.__snPreviewResult" in html

    def test_decks_picker_present(self):
        html = build_smart_notes_html(dark=False)
        assert "sn-decks-modal" in html  # the searchable deck-scope popup
        assert "sn-decks-search" in html
        assert "selectedDeckIds" in html  # the JS helper read into the payloads
        assert (
            "buildDeckTree" in html and "sn-deck-tog" in html
        )  # collapsible hierarchy

    def test_toggle_all_headers_and_options_present(self):
        html = build_smart_notes_html(dark=False)
        assert 'data-toggle="generate"' in html and "sn-th-toggle" in html
        assert "sn-options-modal" in html and "⚙ Options" in html
        assert "toggleAllColumn" in html  # the header click handler

    def test_field_sort_present(self):
        html = build_smart_notes_html(dark=False)
        assert "sn-sort-field" in html and "sortByField" in html

    def test_tabbed_options_with_account_markup(self):
        html = build_smart_notes_html(dark=False)
        assert "sn-tabs" in html and 'data-tab="account"' in html
        assert "sn-acct-usage" in html
        assert 'id="sn-acct-run"' in html

    def test_account_ops_and_push_hooks_wired(self):
        html = build_smart_notes_html(dark=False)
        for op in ("account_data", "account_credit", "account_test"):
            assert op in html
        assert "window.__snAccountTestResult" in html
        assert "window.__snCreditResult" in html

    def test_auto_detect_voices_editor_baked(self):
        html = build_smart_notes_html(dark=False)
        # The catalog bakes the per-language Auto-detect options the editor populates from.
        assert "auto_voice_options" in html
        # The new ops + push hook + Refresh button are wired in.
        for op in ("set_auto_voice", "refresh_voices"):
            assert op in html
        assert "window.__snVoicesRefreshed" in html
        assert "Refresh voices" in html

    def test_native_runtimes_panel_present(self):
        html = build_smart_notes_html(dark=False)
        # The Options → General "Native runtimes" panel: its container + the op + push hooks.
        assert "Native runtimes" in html and "sn-native-list" in html
        assert "set_native_runtime" in html and "native_runtimes" in html
        assert "window.__snNativeRuntimeProgress" in html
        assert "window.__snNativeRuntimeDone" in html

    def test_per_field_voice_empty_option_reads_auto_detect(self):
        # The per-row Voice picker's blank option is relabeled to "Auto-detect" in rebuildVoice.
        html = build_smart_notes_html(dark=False)
        assert 'label: "Auto-detect"' in html

    def test_classify_deps_op_and_push_hook_wired(self):
        # Feature 1 (prompt → graph): modalSave posts classify_deps; the result recolours via
        # window.__snDepsResult, applied per field through applyFieldDeps.
        html = build_smart_notes_html(dark=False)
        assert 'send(\n      "classify_deps"' in html or '"classify_deps"' in html
        assert "window.__snDepsResult" in html
        assert "function applyFieldDeps" in html

    def test_auto_and_improve_apply_optional_deps_map(self):
        # The auto/improve folds carry an optional res.deps map the page applies per field, and
        # append the "Updated dependency colours" sentence to the completion message.
        html = build_smart_notes_html(dark=False)
        assert "applyDepsMap" in html
        assert "res.deps" in html
        assert "Updated dependency colours for" in html

    def test_graph_to_prompt_ops_and_hooks_wired(self):
        # Feature 2 (graph → prompt): Save reconciles changed edges via rewrite_edges; the popover's
        # live guard rail posts validate_prompt; its ✨ Improve posts improve_prompt_pinned. The
        # off-thread results push via window.__snRewriteResult / window.__snImproveResult.
        html = build_smart_notes_html(dark=False)
        for op in ("validate_prompt", "rewrite_edges", "improve_prompt_pinned"):
            assert op in html
        assert "window.__snRewriteResult" in html
        # The popover's pinned improve uses a DEDICATED hook (not the editor's shared one) so a
        # stale result can never write an unverified prompt onto a row (W1).
        assert "window.__snDiffImproveResult" in html
        assert "openDiffPopover" in html

    def test_save_folds_in_edge_sync(self):
        # The former "↻ Sync prompts" button is gone; Save (beginSaveWithSync) reconciles any
        # changed edges — including pending derived deletions (removedEdges) — before performSave
        # persists the config.
        html = build_smart_notes_html(dark=False)
        assert "sn-graph-reload" not in html  # the separate Sync-prompts control is removed
        assert "removedEdges" in html
        assert "function beginSaveWithSync" in html
        assert "function performSave" in html
        assert "beginSaveWithSync" in html  # wired as the Save click handler

    def test_diff_popover_markup_present(self):
        # The compact floating "Was → Now" diff card (NOT the full-screen modal).
        html = build_smart_notes_html(dark=False)
        assert "sn-diff-pop" in html and "sn-diff-card" in html
        assert "sn-diff-old" in html and "sn-diff-new" in html
        assert "sn-diff-apply" in html and "sn-diff-improve" in html

    def test_node_prompt_tooltip_present(self):
        # Feature 1: a styled hover tooltip (its element + CSS + the show helper) replaces <title>.
        html = build_smart_notes_html(dark=False)
        assert 'id="sn-node-tip"' in html and "sn-tip-name" in html
        assert "showNodeTip" in html and ".sn-node-tip" in html

    def test_node_positions_persisted_in_payloads(self):
        # Feature 2: positions ride on graph_recompute + save; collectPositions is the hoisted
        # source, seeded from the graph's node_positions.
        html = build_smart_notes_html(dark=False)
        assert "function collectPositions" in html
        assert "savedPositions" in html
        assert "positions: collectPositions()" in html
        assert "node_positions" in html

    def test_border_connect_and_routing_present(self):
        # Feature 3: dynamic border anchoring + the text-zone interaction (drag the label to move,
        # drag elsewhere to connect); no fixed connector port.
        html = build_smart_notes_html(dark=False)
        assert "borderPoint" in html and "nodeCenter" in html
        assert "function isOverText" in html  # the label = move zone, elsewhere = connect + tip
        assert "sn-handle" not in html  # the fixed connector dot is gone
        assert "border" in html  # the updated hint text

    def test_lock_integration_present(self):
        # Feature 4: lock badge + unlock control, edge-edit guard, and the unlock helper.
        html = build_smart_notes_html(dark=False)
        assert "sn-node-locked" in html and "sn-unlock-btn" in html
        assert "function unlockField" in html and "function isFieldLocked" in html
        assert "is locked — unlock it to change its dependencies." in html
