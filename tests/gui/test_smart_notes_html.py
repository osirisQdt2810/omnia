"""Tests for the pure Smart Notes HTML builder + row↔config mapping (no Qt/aqt)."""

from __future__ import annotations

import json

from omnia.gui.smart_notes.html import (
    build_smart_notes_html,
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
