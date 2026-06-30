"""Tests for the smart-notes prompt↔graph consistency seam (engine/consistency.py).

Pure logic — no Anki. Covers the syntax authority (``validate_prompt_syntax``, cloze-aware),
incoming-edge derivation (``NodeEdgeSet.derive`` and ``FieldGraph.node_edge_set``, including the
B3 empty-prompt rule, kind overrides, dropped + self edges, explicit-only edges), and the diff
(``NodeEdgeSet.diff``: added/removed/kind-change reporting, ``ok`` semantics, bad syntax).
"""

from __future__ import annotations

from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine import (
    FieldGraph,
    NodeEdgeSet,
    validate_prompt_syntax,
)

_KNOWN = ["Word", "Definition", "Note", "Audio"]


def _edges(target, prompt, *, depends_on=None, known=None):
    """Build a NodeEdgeSet and return its edge set (the common assertion surface)."""
    result = NodeEdgeSet.derive(
        target, prompt, list(depends_on or []), known if known is not None else _KNOWN
    )
    return result.edges


class TestFromConfigDeriveAgreement:
    """Linchpin invariant: the graph-built edge set (``FieldGraph.node_edge_set``) and the
    candidate-prompt-derived set (``NodeEdgeSet.derive``) must AGREE for every field, so the two
    sync directions can never drift. Covers derived-hard, ref+explicit-soft override, B3 empty
    prompt, and an explicit-only edge in one config."""

    @staticmethod
    def _config() -> SmartNotesNoteTypeConfig:
        return SmartNotesNoteTypeConfig(
            note_type="Vocab",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                ),
                SmartNotesFieldConfig(
                    field="Note",
                    enabled=True,
                    type="text",
                    prompt="{{Word}} — {{Definition}}",
                    depends_on=[FieldDep(field="Word", kind="soft")],
                ),
                SmartNotesFieldConfig(
                    field="Audio", enabled=True, type="tts", prompt=""
                ),  # B3: empty prompt → no incoming edge
                SmartNotesFieldConfig(
                    field="Extra",
                    enabled=True,
                    type="text",
                    prompt="hi",  # non-empty, no refs
                    depends_on=[
                        FieldDep(field="Definition", kind="hard")
                    ],  # explicit-only
                ),
            ],
        )

    def test_graph_and_derive_agree_per_field(self):
        cfg = self._config()
        graph = FieldGraph.from_config(cfg)
        known = ["Word"] + [f.field for f in cfg.fields]
        for field in cfg.fields:
            from_graph = graph.node_edge_set(field.field).edges
            from_prompt = NodeEdgeSet.derive(
                field.field, field.prompt, list(field.depends_on), known
            ).edges
            assert (
                from_graph == from_prompt
            ), f"drift at {field.field}: {from_graph} != {from_prompt}"


class TestAutoProvenanceSurvivesRecompute:
    """B1: a classifier-written (auto=True) SOFT edge must survive FieldGraph.from_config as
    soft — NOT revert to the derived-default hard. This locks the persistence contract the
    prompt→graph classifier relies on against future `auto`-handling changes."""

    def test_auto_soft_edge_round_trips_as_soft(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Vocab",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",  # {{Word}} would derive HARD by default
                    depends_on=[FieldDep(field="Word", kind="soft", auto=True)],
                )
            ],
        )
        edges = FieldGraph.from_config(config).node_edge_set("Definition").edges
        assert edges == frozenset({("word", "soft")})


class TestValidatePromptSyntax:
    def test_unclosed_open_braces_error(self):
        assert validate_prompt_syntax("{{FIELD") != []

    def test_unopened_close_braces_error(self):
        assert validate_prompt_syntax("FIELD}}") != []

    def test_empty_placeholder_errors(self):
        assert validate_prompt_syntax("{{}}") != []
        assert validate_prompt_syntax("{{ }}") != []

    def test_cloze_deletion_is_not_flagged(self):
        assert validate_prompt_syntax("{{c1::x}}") == []

    def test_valid_field_ref_has_no_errors(self):
        assert validate_prompt_syntax("Define {{Word}}") == []

    def test_garbled_nested_braces_are_handled(self):
        # "{{Wo{{rd}}" must not crash and must report the malformed leading "{{".
        assert validate_prompt_syntax("{{Wo{{rd}}") != []

    def test_empty_prompt_is_valid(self):
        assert validate_prompt_syntax("") == []


class TestNodeEdgeSet:
    def test_empty_prompt_yields_no_edges(self):
        # B3: an empty prompt reads the base only as a generation fallback, not a dependency.
        assert _edges("Definition", "") == frozenset()

    def test_field_ref_is_a_hard_edge(self):
        assert _edges("Definition", "{{Word}}") == frozenset({("word", "hard")})

    def test_explicit_soft_override_flips_kind(self):
        edges = _edges(
            "Definition",
            "{{Word}}",
            depends_on=[FieldDep(field="Word", kind="soft")],
        )
        assert edges == frozenset({("word", "soft")})

    def test_edge_to_unknown_field_is_dropped(self):
        assert _edges("Definition", "{{Ghost}}") == frozenset()

    def test_self_reference_is_dropped(self):
        assert _edges("Definition", "{{Definition}}") == frozenset()

    def test_explicit_only_edge_is_included(self):
        # Note is not a {{ref}} but an explicit depends_on edge — it is still an incoming edge.
        edges = _edges(
            "Definition",
            "static text",
            depends_on=[FieldDep(field="Note", kind="hard")],
        )
        assert edges == frozenset({("note", "hard")})

    def test_union_of_derived_and_explicit_edges(self):
        edges = _edges(
            "Definition",
            "{{Word}}",
            depends_on=[FieldDep(field="Note", kind="soft")],
        )
        assert edges == frozenset({("word", "hard"), ("note", "soft")})

    def test_matching_is_case_insensitive(self):
        assert _edges("Definition", "{{word}}") == frozenset({("word", "hard")})

    def test_syntax_errors_are_carried(self):
        result = NodeEdgeSet.derive("Definition", "{{FIELD", [], _KNOWN)
        assert result.syntax_errors != ()


class TestFieldGraphNodeEdgeSet:
    def _graph(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(field="Note", enabled=True, type="text"),
                SmartNotesFieldConfig(
                    field="Definition",
                    enabled=True,
                    type="text",
                    prompt="Define {{Word}}",
                    depends_on=[FieldDep(field="Note", kind="soft")],
                ),
            ],
        )
        return FieldGraph.from_config(config)

    def test_reads_incoming_edges_from_the_graph(self):
        result = self._graph().node_edge_set("Definition")
        assert result.edges == frozenset({("word", "hard"), ("note", "soft")})

    def test_unknown_target_yields_empty_set(self):
        result = self._graph().node_edge_set("Ghost")
        assert result.edges == frozenset()


class TestNodeEdgeSetDiff:
    def test_identical_sets_are_ok_with_no_messages(self):
        before = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        after = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        result = before.diff(after)
        assert result.ok is True
        assert result.messages == ()
        assert result.added_fields == ()
        assert result.removed_fields == ()

    def test_added_ref_reports_and_is_not_ok(self):
        before = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        after = NodeEdgeSet.derive("Definition", "{{Word}} {{Note}}", [], _KNOWN)
        result = before.diff(after)
        assert result.ok is False
        assert result.added_fields == ("note",)
        assert any(m == 'Added dependency on "note"' for m in result.messages)

    def test_removed_ref_reports_and_is_not_ok(self):
        before = NodeEdgeSet.derive("Definition", "{{Word}} {{Note}}", [], _KNOWN)
        after = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        result = before.diff(after)
        assert result.ok is False
        assert result.removed_fields == ("note",)
        assert any(m == 'Removed dependency on "note"' for m in result.messages)

    def test_kind_change_is_reported_but_stays_ok(self):
        # A kind flip (hard -> soft) is reported, but per the documented contract it does NOT by
        # itself set ok=False — the caller decides whether a recolour is acceptable.
        before = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        after = NodeEdgeSet.derive(
            "Definition", "{{Word}}", [FieldDep(field="Word", kind="soft")], _KNOWN
        )
        result = before.diff(after)
        assert result.kind_changes == (("word", "hard", "soft"),)
        assert result.added_fields == ()
        assert result.removed_fields == ()
        assert result.ok is True

    def test_bad_after_syntax_sets_bad_syntax(self):
        before = NodeEdgeSet.derive("Definition", "{{Word}}", [], _KNOWN)
        after = NodeEdgeSet.derive("Definition", "{{Word", [], _KNOWN)
        result = before.diff(after)
        assert result.bad_syntax is True
        assert result.ok is False
        assert result.messages != ()
