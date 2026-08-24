"""Renaming a note type's fields must reach EVERY place a field name is written down.

Each test here stands for one of those places. They are separate tests rather than one big
assertion because the failure they guard against is partial success: a remap that fixes the
rule names and misses the prompts produces a configuration that loads, renders, and generates
the wrong thing — which is far harder to notice than one that fails outright.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.config import (
    FieldDep,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.transfer.remap import (
    identity_renames,
    remap_note_type_config,
    suggest_renames,
)


def _config(**overrides) -> SmartNotesNoteTypeConfig:
    base = dict(
        note_type="Source",
        base_field="Word",
        node_positions={"Definition": [10.0, 20.0], "Word": [0.0, 0.0]},
        fields=[
            SmartNotesFieldConfig(
                field="Definition",
                enabled=True,
                prompt="Define {{Word}} using {{Example}}.",
                depends_on=[FieldDep(field="Example", kind="hard", auto=True)],
                tools=[
                    FieldToolConfig(
                        tool="cloze",
                        params={
                            "sentence_field": "Example",
                            "word_field": "Word",
                            "mask": "none",
                        },
                    )
                ],
            ),
            SmartNotesFieldConfig(
                field="Example", enabled=True, prompt="A sentence for {{Word}}"
            ),
        ],
    )
    base.update(overrides)
    return SmartNotesNoteTypeConfig(**base)


RENAMES = {"Word": "Term", "Definition": "Meaning", "Example": "Sentence"}


class TestEveryPlaceAFieldNameIsWritten:
    def test_the_rule_target(self):
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert [rule.field for rule in out.fields] == ["Meaning", "Sentence"]

    def test_the_base_field(self):
        """Miss this and every rule loses its source, so nothing generates at all."""
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert out.base_field == "Term"

    def test_the_prompt_placeholders(self):
        """Miss these and the model is handed a placeholder that interpolates to nothing."""
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert out.fields[0].prompt == "Define {{Term}} using {{Sentence}}."
        assert out.fields[1].prompt == "A sentence for {{Term}}"

    def test_the_dependency_edges(self):
        """A stale HARD edge blocks generation forever and shows no reason why."""
        out, _ = remap_note_type_config(_config(), RENAMES)
        dep = out.fields[0].depends_on[0]

        assert (dep.field, dep.kind, dep.auto) == ("Sentence", "hard", True)

    def test_the_graph_node_positions(self):
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert out.node_positions == {"Meaning": [10.0, 20.0], "Term": [0.0, 0.0]}

    def test_the_tool_params_that_name_fields(self):
        """The trap: the param KEY differs per tool, so there is no fixed list to rewrite."""
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert out.fields[0].tools[0].params == {
            "sentence_field": "Sentence",
            "word_field": "Term",
            "mask": "none",
        }

    def test_a_tool_param_that_is_not_a_field_is_left_alone(self):
        """``mask="none"`` is a mode, not a field. Rewriting it would break the tool."""
        out, _ = remap_note_type_config(
            _config(), {**RENAMES, "none": "SOMETHING ELSE"}
        )

        assert out.fields[0].tools[0].params["mask"] == "none"

    def test_the_note_type_name_when_asked(self):
        out, _ = remap_note_type_config(_config(), RENAMES, note_type_name="Target")

        assert out.note_type == "Target"

    def test_the_note_type_name_is_kept_when_not_asked(self):
        out, _ = remap_note_type_config(_config(), RENAMES)

        assert out.note_type == "Source"


class TestAnAnkiClozeIsNotAFieldReference:
    def test_cloze_deletions_survive_a_remap(self):
        """``{{c1::…}}`` is Anki's own syntax; rewriting it would corrupt the card."""
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition", prompt="{{c1::Word}} and {{Word}}"
                )
            ]
        )

        out, _ = remap_note_type_config(config, RENAMES)

        assert out.fields[0].prompt == "{{c1::Word}} and {{Term}}"


class TestFieldsWithNoCounterpart:
    def test_an_unmapped_rule_is_dropped_and_reported(self):
        """A rule targeting a field the note type does not have can never generate, and shows
        as a row in the Fields table the user cannot act on."""
        out, report = remap_note_type_config(
            _config(), {"Word": "Term", "Definition": "Meaning"}
        )

        assert [rule.field for rule in out.fields] == ["Meaning"]
        assert report.dropped_fields == ["Example"]
        assert report.has_warnings

    def test_an_edge_onto_a_dropped_field_goes_with_it(self):
        out, report = remap_note_type_config(
            _config(), {"Word": "Term", "Definition": "Meaning"}
        )

        assert out.fields[0].depends_on == []
        assert report.dropped_dependencies == ["Definition -> Example"]

    def test_keep_unmapped_keeps_the_name_as_it_was(self):
        out, report = remap_note_type_config(
            _config(), {"Word": "Term"}, keep_unmapped=True
        )

        assert [rule.field for rule in out.fields] == ["Definition", "Example"]
        assert report.dropped_fields == []

    def test_a_dropped_base_field_is_reported(self):
        out, report = remap_note_type_config(_config(), {"Definition": "Meaning"})

        assert out.base_field == ""
        assert "Word" in report.dropped_fields


class TestAToolThatDeclaresNothing:
    def test_its_field_looking_param_is_reported_not_guessed(self, monkeypatch):
        """A user tool that never overrode ``referenced_fields`` cannot be remapped safely.
        Rewriting on a guess breaks a chain that stored a literal; staying silent hands the
        user a tool reading a field that no longer exists. So: report it."""
        from omnia.plugins.smart_notes.engine.tools import registry
        from omnia.plugins.smart_notes.engine.tools.base import Tool

        class Undeclaring(Tool):
            """A user tool that inherits ``referenced_fields`` instead of overriding it."""

            def run(self, request, ctx):  # pragma: no cover - never invoked here
                raise NotImplementedError

        monkeypatch.setattr(registry, "get_tool", lambda name: Undeclaring)
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[
                        FieldToolConfig(tool="user:mystery", params={"src": "Example"})
                    ],
                )
            ]
        )

        out, report = remap_note_type_config(config, RENAMES)

        assert out.fields[0].tools[0].params == {"src": "Example"}  # untouched
        assert any("user:mystery.src" in note for note in report.unchecked_tool_params)

    def test_an_unknown_tool_keeps_its_params(self, monkeypatch):
        """A chain may name a tool this machine does not have; it must still round-trip."""
        from omnia.plugins.smart_notes.engine.tools import registry

        monkeypatch.setattr(registry, "get_tool", lambda name: None)
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[FieldToolConfig(tool="not:here", params={"a": "Example"})],
                )
            ]
        )

        out, _ = remap_note_type_config(config, RENAMES)

        assert out.fields[0].tools[0].params == {"a": "Example"}


class TestTheMappingHelpers:
    def test_identity_covers_the_base_field_too(self):
        mapping = identity_renames(_config())

        assert mapping == {
            "Definition": "Definition",
            "Example": "Example",
            "Word": "Word",
        }

    def test_identity_remap_changes_nothing(self):
        config = _config()

        out, report = remap_note_type_config(config, identity_renames(config))

        assert out.dict() == config.dict()
        assert not report.has_warnings

    def test_suggest_pairs_exact_names_first(self):
        mapping = suggest_renames(["Word", "Example"], ["Example", "Word", "Extra"])

        assert mapping == {"Word": "Word", "Example": "Example"}

    def test_suggest_falls_back_to_case_insensitive(self):
        mapping = suggest_renames(["Word"], ["word"])

        assert mapping == {"Word": "word"}

    def test_suggest_leaves_an_ambiguous_pairing_alone(self):
        """Two candidates differing only by case: guessing would rewrite prompts and tool
        params onto the wrong field, which is expensive to notice."""
        mapping = suggest_renames(["Word"], ["word", "WORD"])

        assert mapping == {}

    def test_suggest_never_reuses_a_target(self):
        mapping = suggest_renames(["Word", "word"], ["Word"])

        assert mapping == {"Word": "Word"}


class TestAToolParamWrittenLooselyIsStillTheSameField:
    """A tool's ``referenced_fields`` strips its params and the dependency graph matches field
    names case-insensitively — so a param stored as `` Sentence`` or ``sentence`` is the same
    field to everything else. Matching it raw would leave it neither rewritten nor reported.
    """

    def _with_param(self, stored: str):
        return _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[
                        FieldToolConfig(
                            tool="cloze",
                            params={"sentence_field": stored, "word_field": "Word"},
                        )
                    ],
                )
            ]
        )

    def test_a_padded_param_is_rewritten(self):
        out, report = remap_note_type_config(self._with_param("  Example  "), RENAMES)

        assert out.fields[0].tools[0].params["sentence_field"] == "Sentence"
        assert report.unchecked_tool_params == []

    def test_a_differently_cased_param_is_rewritten(self):
        out, report = remap_note_type_config(self._with_param("EXAMPLE"), RENAMES)

        assert out.fields[0].tools[0].params["sentence_field"] == "Sentence"
        assert report.unchecked_tool_params == []

    def test_a_rename_that_only_changes_case_still_moves_it(self):
        out, _ = remap_note_type_config(
            self._with_param("Example"),
            {"Example": "EXAMPLE", "Word": "Word", "Definition": "Definition"},
        )

        assert out.fields[0].tools[0].params["sentence_field"] == "EXAMPLE"

    def test_a_param_naming_a_field_that_is_not_moving_is_left_alone(self):
        out, report = remap_note_type_config(
            self._with_param("Example"),
            {"Example": "Example", "Word": "Word", "Definition": "Definition"},
        )

        assert out.fields[0].tools[0].params["sentence_field"] == "Example"
        assert report.unchecked_tool_params == []

    def test_a_tool_param_naming_a_dropped_field_is_reported(self):
        """The rule survives (its own field mapped), so no dropped-rule or dropped-edge line
        would mention it — and the tool is now reading a field this note type does not have.
        """
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[
                        FieldToolConfig(
                            tool="cloze",
                            params={"sentence_field": "Example", "word_field": "Word"},
                        )
                    ],
                )
            ]
        )

        out, report = remap_note_type_config(
            config, {"Word": "Term", "Definition": "Meaning"}
        )

        assert out.fields[0].tools[0].params["sentence_field"] == "Example"
        assert report.dropped_tool_params == [
            "Definition: cloze.sentence_field = 'Example'"
        ]
        assert report.has_warnings

    def test_a_param_holding_a_literal_is_not_reported(self):
        """``declared`` comes from the tool itself, so a mode like ``mask="none"`` cannot
        trip this even though "none" has no counterpart in the mapping."""
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[
                        FieldToolConfig(
                            tool="cloze",
                            params={
                                "sentence_field": "Example",
                                "word_field": "Word",
                                "mask": "none",
                            },
                        )
                    ],
                )
            ]
        )

        _out, report = remap_note_type_config(config, RENAMES)

        assert report.dropped_tool_params == []

    def test_keep_unmapped_does_not_report_it(self):
        """Nothing was dropped: the field it names stays exactly as it was."""
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    tools=[
                        FieldToolConfig(
                            tool="cloze", params={"sentence_field": "Example"}
                        )
                    ],
                )
            ]
        )

        _out, report = remap_note_type_config(
            config, {"Definition": "Meaning"}, keep_unmapped=True
        )

        assert report.dropped_tool_params == []
