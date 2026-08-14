"""Tests for the per-row Tools picker: the payload contract and the page it is rendered by.

A field's tool chain has to survive FIVE places that each know its shape, and a chain silently
drops if ANY one of them forgets it:

1. ``collectRows`` (web/03-render.js) — the row must POST its chain;
2. ``row_to_payload`` (gui/smart_notes/html.py) — a loaded row must CARRY it to the page;
3. ``field_configs_from_payload`` (same file) — the posted chain must be PARSED, while a
   payload that carries no ``tools`` key still falls back to the stored one;
4. ``SmartNotesFieldConfig.tools`` — it must PERSIST;
5. the row Preview path (``dialogs/controllers/authoring.py``) — a preview must run the row's
   OWN chain, or a "cloze → ai" row previews the AI it never reaches in a real run.

:class:`TestFiveSyncPoints` walks the whole loop so that missing any one of them fails here.
The two JS sync points are asserted against the built page source, which is how this suite has
always covered the page (there is no JS runner); Python owns everything else.
"""

from __future__ import annotations

import json
import re

from omnia.gui.smart_notes.html import (
    build_smart_notes_html,
    field_configs_from_payload,
    load_payload,
    note_type_config_from_payload,
    row_to_payload,
)
from omnia.plugins.smart_notes.config import (
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine import compile_field_rule


def _row(field: str, **kw) -> dict:
    """One posted row payload, as ``collectRows`` builds it."""
    base = {
        "field": field,
        "enabled": True,
        "type": "text",
        "prompt": "",
        "prompt_locked": False,
        "provider": "",
        "model": "",
        "voice": "",
        "language": "",
        "overwrite": False,
        "depends_on": [],
        "tools": [],
    }
    base.update(kw)
    return base


def _chain(*tools: dict) -> list[dict]:
    return list(tools)


_CLOZE_AI = _chain(
    {"tool": "cloze", "params": {"sentence_field": "Sentence"}},
    {"tool": "ai", "params": {}},
)


def _js(html: str, marker: str, length: int = 1400) -> str:
    """The slice of the built page starting at ``marker`` (a function/handler to inspect)."""
    start = html.index(marker)
    return html[start : start + length]


class TestFiveSyncPoints:
    """One chain, all the way around the loop and back — and into the preview path."""

    def test_a_chain_survives_page_to_config_to_page(self):
        rows = [_row("Cloze", tools=_CLOZE_AI)]

        config = note_type_config_from_payload("Vocab", "Word", rows)
        stored = config.fields[0]
        # 3 + 4: parsed off the payload and persisted on the model…
        assert [entry.tool for entry in stored.tools] == ["cloze", "ai"]
        assert stored.tools[0].params == {"sentence_field": "Sentence"}
        assert stored.dict()["tools"][0]["tool"] == "cloze"
        # 2: …and handed back to the page unchanged.
        assert row_to_payload(stored)["tools"] == _CLOZE_AI
        assert [row_to_payload(row) for row in config.fields] == rows

    def test_load_payload_carries_the_saved_chain_to_the_first_paint(self):
        saved = SmartNotesFieldConfig(
            field="Cloze",
            enabled=True,
            tools=[
                FieldToolConfig(tool="cloze", params={"sentence_field": "Sentence"})
            ],
        )
        payload = load_payload(
            "Vocab",
            SmartNotesNoteTypeConfig(
                note_type="Vocab", base_field="Word", fields=[saved]
            ),
            ["Word", "Cloze"],
            ["gemini"],
        )
        assert payload["rows"][0]["tools"] == [
            {"tool": "cloze", "params": {"sentence_field": "Sentence"}}
        ]

    def test_the_preview_path_compiles_the_posted_chain(self):
        # 5: the Preview op builds its rule through field_configs_from_payload +
        # compile_field_rule, so the row's chain (and base field) reach the pipeline.
        posted = _row("Cloze", prompt="Cloze {{Sentence}}", tools=_CLOZE_AI)

        rule = compile_field_rule(field_configs_from_payload([posted])[0], "Word")

        assert [spec.name for spec in rule.tools] == ["cloze", "ai"]
        assert rule.tools[0].params == {"sentence_field": "Sentence"}
        assert rule.base_field == "Word"

    def test_a_preview_payload_without_a_chain_still_compiles_to_ai(self):
        rule = compile_field_rule(field_configs_from_payload([_row("Def")])[0], "Word")
        assert [spec.name for spec in rule.tools] == ["ai"]

    def test_collect_rows_posts_the_chain(self):
        # 1: the JS sync point. collectRows must emit `tools` — without it every save would
        # look like "this page cannot render chains" and silently keep the stored value.
        collect = _js(build_smart_notes_html(dark=False), "function collectRows()")
        assert "tools: readTools(tr)" in collect

    def test_the_preview_payload_posts_the_chain(self):
        payload = _js(build_smart_notes_html(dark=False), "function previewPayload(")
        assert "tools: readTools(tr)" in payload


class TestChainPayloadParsing:
    """``field_configs_from_payload`` must be tolerant, and must not confuse absent with empty."""

    def _stored(self) -> SmartNotesFieldConfig:
        return SmartNotesFieldConfig(
            field="Cloze", enabled=True, tools=[FieldToolConfig(tool="cloze")]
        )

    def test_a_posted_empty_chain_clears_the_stored_one(self):
        stored = self._stored()
        rebuilt = field_configs_from_payload([_row("Cloze", tools=[])], [stored])
        assert rebuilt[0].tools == []

    def test_a_payload_with_no_tools_key_keeps_the_stored_chain(self):
        stored = self._stored()
        posted = _row("Cloze")
        posted.pop("tools")

        rebuilt = field_configs_from_payload([posted], [stored])

        assert [entry.tool for entry in rebuilt[0].tools] == ["cloze"]

    def test_an_unknown_tool_name_is_kept_verbatim(self):
        # A user tool authored on another device: this build cannot run it (the pipeline
        # degrades it to unknown_tool), but dropping the name here would delete it on sync.
        rebuilt = field_configs_from_payload(
            [
                _row(
                    "Cloze",
                    tools=_chain({"tool": "user:extract-ext", "params": {"x": 1}}),
                )
            ]
        )
        assert rebuilt[0].tools[0].tool == "user:extract-ext"
        assert rebuilt[0].tools[0].params == {"x": 1}

    def test_malformed_entries_are_dropped_without_raising(self):
        rebuilt = field_configs_from_payload(
            [
                _row(
                    "Cloze",
                    tools=[
                        "cloze",  # not a dict
                        {"params": {"a": 1}},  # no tool name
                        {"tool": "  "},  # blank tool name
                        {"tool": "ai", "params": "nope"},  # params not a dict
                    ],
                )
            ]
        )
        assert [entry.tool for entry in rebuilt[0].tools] == ["ai"]
        assert rebuilt[0].tools[0].params == {}

    def test_a_non_list_tools_value_yields_an_empty_chain(self):
        rebuilt = field_configs_from_payload([_row("Cloze", tools="cloze")])
        assert rebuilt[0].tools == []


class TestToolsPickerPage:
    """The column, the modal, the baked catalog, and the behaviours only the page owns."""

    def _html(self, tools=None) -> str:
        return build_smart_notes_html(dark=False, tools=tools)

    def test_the_tools_column_sits_between_prompt_and_provider(self):
        html = self._html()
        assert ">Tools</th>" in html
        assert (
            html.index(">Prompt</th>")
            < html.index(">Tools</th>")
            < html.index(">Provider</th>")
        )

    def test_the_picker_modal_is_present(self):
        html = self._html()
        for marker in (
            "sn-tools-modal",
            "sn-tools-list",
            "sn-tools-done",
            "sn-tools-cancel",
        ):
            assert marker in html

    def test_the_catalog_is_baked_for_the_first_paint(self):
        catalog = [
            {
                "name": "cloze",
                "label": "Cloze",
                "description": "Wrap the word.",
                "kinds": ["text"],
                "deterministic": True,
                "params_schema": {"properties": {"sentence_field": {"type": "string"}}},
                "unavailable_reason": None,
            }
        ]
        html = self._html(tools=catalog)
        assert "window.__SN_TOOLS" in html
        assert json.dumps(catalog) in html

    def test_no_catalog_bakes_an_empty_list(self):
        # A dialog that could not build a provider hub still renders; the picker says so.
        assert "window.__SN_TOOLS = [];" in self._html()
        assert "No tool can generate this field type" in self._html()

    def test_the_row_seeds_and_reads_its_chain_from_one_place(self):
        html = self._html()
        assert "tr.dataset.tools = JSON.stringify(row.tools || []);" in html
        assert "function readTools(tr)" in html

    def test_changing_the_type_resets_the_chain(self):
        # Tools are kind-scoped (cloze is text-only), so a chain must not survive a type switch —
        # mirroring the model/voice reset right beside it.
        kind_change = _js(build_smart_notes_html(dark=False), "function onKindChange(")
        assert re.search(r"writeTools\(tr, \[\]\);", kind_change)

    def test_an_unusable_tool_is_rendered_greyed_with_its_reason(self):
        html = self._html()
        assert "sn-tool-unavailable" in html
        assert "Not installed on this device" in html
        # An unusable tool cannot be ADDED, but one already in the chain stays removable.
        assert "cb.disabled = !!blocked && index < 0;" in html

    def test_what_a_tool_reports_missing_is_advice_and_never_gates_it(self):
        # cloze_audio works with a WAV voice and NOTHING installed, and it cannot see which
        # voice a row will resolve to — so gating on its report would lock a zero-install user
        # out of the tool entirely. Only "not installed here" and "wrong field type" gate.
        html = self._html()
        blocked = _js(html, "function toolBlocked(", 330)
        assert "spec.unavailable_reason" not in blocked
        advice = _js(html, "function toolAdvice(", 200)
        assert "spec.unavailable_reason" in advice
        assert 'note.className = "sn-tool-note";' in html

    def test_the_picker_warns_when_a_voice_tool_runs_after_cloze_audio(self):
        # Graft #1. On THIS build the chain is safe (cloze_audio fails terminally and stops
        # it), but the config syncs: an Omnia without the tool skips the entry and the next TTS
        # tool reads the sentence out WITH the answer. That residual is a warning, not a block.
        html = self._html()
        speakers = _js(html, "function speakersAfterClozeAudio(", 700)
        assert 'entry.tool === "cloze_audio"' in speakers
        assert '(spec.kinds || []).indexOf("tts") >= 0' in speakers
        assert "sn-tools-warn" in html
        assert "reads the answer aloud" in html

    def test_the_picker_is_frozen_on_a_locked_row(self):
        lock_state = _js(build_smart_notes_html(dark=False), "function applyLockState(")
        assert '"sn-tools-btn"' in lock_state

    def test_params_are_merged_not_rebuilt(self):
        # A param this build cannot render (a newer release's) must survive Done: the picker
        # edits a COPY of the stored params object rather than building a fresh one.
        html = self._html()
        assert "params[key] = entry.params[key];" in html
