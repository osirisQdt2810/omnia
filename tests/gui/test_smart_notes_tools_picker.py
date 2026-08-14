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


def _strip_comments(source: str) -> str:
    """Return ``source`` with ``//`` line comments removed.

    These tests assert on the built page's JS as text, and this file is heavily commented —
    including comments that NAME the very class or keyword an assertion checks is absent. Two
    of them passed or failed on prose rather than code before this.
    """
    return "\n".join(
        line.split("//")[0] if "//" in line else line for line in source.splitlines()
    )


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

    def test_done_refuses_a_chain_with_a_required_param_left_blank(self):
        # Generic: WHICH params matter is the tool's declaration (`required_params` in the
        # catalog), so a user-authored tool gets the same gate with no page changes. Blank is
        # exactly the state a fallback would silently resolve, and the picker cannot show which
        # field that fallback picks — so it refuses instead of accepting an unreadable row.
        html = self._html()
        missing = _js(html, "function missingRequired(", 900)
        assert "spec.required_params" in _js(html, "function requiredParams(", 400)
        # A tool this build lacks has no schema to judge; a tool that cannot serve this row's
        # kind never runs. Neither may gate Done.
        assert "!spec ||" in missing
        done = _js(html, 'toolsDone.addEventListener("click"', 600)
        assert "if (missingRequired().length) {" in done
        assert "return;" in done  # neither written nor closed
        assert "sn-tools-warn" in html
        assert "sn-tool-param-missing" in html

    def test_the_required_error_appears_only_after_a_rejected_done(self):
        # Flagging every blank the moment a tool is ticked shouts before the user has had a
        # chance to fill anything in; once Done has been refused it stays live and self-clears.
        html = self._html()
        assert "toolsShowErrors = false;" in _js(html, "function openToolsPicker(", 700)
        assert "toolsShowErrors = true;" in _js(
            html, 'toolsDone.addEventListener("click"', 600
        )
        assert "toolsShowErrors ? missingRequiredMessage(" in html

    def test_a_required_field_param_offers_no_blank_option(self):
        # "(default)" IS the rejected state, so leaving it selectable is a trap, not a choice.
        html = self._html()
        assert "? prop.enum" in html
        assert '(required ? [] : [""]).concat(fieldNames())' in html

    def test_no_feature_tools_semantics_live_in_the_picker(self):
        # The first version hard-coded `entry.tool === "cloze_audio"` plus a narrative of that
        # tool's failure mode into shared page infrastructure — which is exactly why the check
        # only covered the ordering that tool happened to be first in. A tool's safety rules
        # belong on the tool; the page reads a declared flag.
        assert "cloze_audio" not in self._html()

    def test_the_lock_freezes_only_what_the_generator_writes(self):
        # Changed deliberately (was: asserts the tools button is frozen too). The lock means one
        # thing — the auto-smart generator must not overwrite a hand-written prompt — and that
        # generator writes exactly `type` and `prompt` (authoring/author.py). The tool chain,
        # provider, model, voice, overwrite and preview are the user's own knobs; freezing them
        # made a locked row unconfigurable for no reason anyone could point at.
        lock_state = _js(build_smart_notes_html(dark=False), "function applyLockState(")

        assert '"sn-type"' in lock_state
        for owned_by_the_user in (
            '"sn-tools-btn"',
            '"sn-provider"',
            '"sn-voice"',
            '"sn-overwrite"',
        ):
            assert owned_by_the_user not in lock_state

    def test_the_lock_does_not_freeze_the_tools_cell_by_css_either(self):
        # The mechanism that ACTUALLY froze the cell, and which a grep of `applyLockState`
        # cannot see: the lock works through the `sn-lockable` class (`pointer-events: none`
        # plus a blur in page.css) and a `sn-row-locked` guard on the click. Dropping the name
        # from `applyLockState` while the cell was still BUILT as `sn-lockable` left the
        # behaviour untouched.
        html = self._html()
        # Comments are stripped first: this file explains WHY the cell is not `sn-lockable`,
        # and an assertion that greps the prose instead of the code fails on its own comment.
        cell = _strip_comments(_js(html, "function makeToolsCell(", 800))

        assert 'cell("sn-tools-cell")' in cell
        assert "sn-lockable" not in cell
        assert "sn-row-locked" not in cell
        # …while the lock still covers what the auto-smart generator writes.
        assert ".sn-row-locked .sn-lockable" in html
        assert 'cell("sn-prompt-cell sn-lockable")' in html

    def test_editing_the_chain_refreshes_the_rows_applicability(self):
        # `applyKindState` ran at row build and on a Type change only — never when the CHAIN
        # changed, which is the one event that decides whether the row reaches a provider. A
        # `cloze`-only row kept its faded Provider/Model after `ai` was appended, and `.sn-na`
        # carries `pointer-events: none`, so those cells could not even be clicked.
        write = _js(self._html(), "function writeTools(", 900)

        assert "applyKindState(tr," in write

    def test_the_fade_asks_the_catalog_not_a_hardcoded_tool_name(self):
        uses_ai = _js(self._html(), "function chainUsesAi(", 900)

        assert "uses_provider" in uses_ai
        assert "deterministic" not in uses_ai  # NOT the inverse — see the Tool contract
        assert "!spec" in uses_ai  # a tool this build lacks cannot be judged away

    def test_a_row_cannot_read_the_field_it_generates(self):
        # A self-edge in the dependency graph, and never what the user meant: `cloze` would be
        # asked to find its word in the very field it is about to overwrite. One click away now
        # that the field params are required and the dropdown holds only real field names.
        names = _js(self._html(), "function fieldNames(", 800)

        assert "toolsRow && toolsRow.dataset.field" in names
        assert "name.toLowerCase() !== own" in names

    def test_deleting_a_tool_edge_repaints_the_graph(self):
        # `from_tool` was tested on the Python side (the payload field) but not in the JS that
        # consumes it. The branch MUTATES the row — clicking a tool edge toggles hard/soft,
        # which writes a real depends_on entry, and Delete drops it — so returning without a
        # repaint left the canvas drawing the old kind while Save persisted the new one.
        remove = _strip_comments(_js(self._html(), "function removeEdge(", 1800))
        branch = remove[remove.index("sel.fromTool") :]

        assert "updateRowDep(sel.dst, sel.src, null)" in branch
        # The repaint must come BEFORE the branch's own `return;` — matching the bare word
        # would hit "returning" in the comment, which is why comments are stripped above.
        assert "recomputeGraph();" in branch
        assert branch.index("recomputeGraph();") < branch.index("return;")

    def test_done_ignores_a_tool_that_cannot_serve_the_row(self):
        # A chain synced from a device whose row had a different Type can hold, say, a tts tool
        # on a text row. The pipeline discards it as `wrong_kind`, so refusing Done over its
        # required params would gate on a tool that can never run.
        missing = _js(self._html(), "function missingRequired(", 1000)

        assert "(spec.kinds || []).indexOf(kind) < 0" in missing

    def test_params_are_merged_not_rebuilt(self):
        # A param this build cannot render (a newer release's) must survive Done: the picker
        # edits a COPY of the stored params object rather than building a fresh one.
        html = self._html()
        assert "params[key] = entry.params[key];" in html
