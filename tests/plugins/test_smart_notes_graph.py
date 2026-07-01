"""Tests for the smart-notes explicit field dependency graph (engine/graph.py).

Pure logic — no Anki. Covers ``FieldGraph.from_config`` (derived/explicit/union edges, kind
override, dropped + case-insensitive edges, base flagging), the cycle guards
(``FieldGraph.validate_acyclic`` / ``would_create_cycle``), and ``FieldGraph.laid_out``
(column/row layout, determinism, termination on a large DAG).
"""

from __future__ import annotations

import pytest

from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine import (
    FieldGraph,
    GraphEdge,
    SmartNotesCycleError,
)


def _config(base, fields):
    """Build a SmartNotesNoteTypeConfig from (field, kwargs) tuples."""
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field=base,
        fields=[SmartNotesFieldConfig(field=name, **kw) for name, kw in fields],
    )


def _edge(graph: FieldGraph, src: str, dst: str) -> GraphEdge | None:
    for edge in graph.edges:
        if edge.src.lower() == src.lower() and edge.dst.lower() == dst.lower():
            return edge
    return None


class TestBuildFieldGraph:
    def test_derived_edge_defaults_to_hard(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))],
            )
        )
        edge = _edge(graph, "Word", "Def")
        assert edge is not None
        assert edge.kind == "hard"
        assert edge.derived is True

    def test_explicit_only_edge_is_not_derived(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("Note", dict(enabled=True, type="text", prompt="static")),
                    (
                        "Def",
                        dict(
                            enabled=True,
                            type="text",
                            prompt="static",
                            depends_on=[FieldDep(field="Note", kind="hard")],
                        ),
                    ),
                ],
            )
        )
        edge = _edge(graph, "Note", "Def")
        assert edge is not None
        assert edge.derived is False
        assert edge.kind == "hard"

    def test_union_keeps_derived_and_explicit_edges(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("Note", dict(enabled=True, type="text", prompt="static")),
                    (
                        "Def",
                        dict(
                            enabled=True,
                            type="text",
                            prompt="define {{Word}}",
                            depends_on=[FieldDep(field="Note", kind="soft")],
                        ),
                    ),
                ],
            )
        )
        assert _edge(graph, "Word", "Def") is not None  # derived
        assert _edge(graph, "Note", "Def") is not None  # explicit

    def test_explicit_kind_overrides_derived(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    (
                        "Def",
                        dict(
                            enabled=True,
                            type="text",
                            prompt="define {{Word}}",
                            depends_on=[FieldDep(field="Word", kind="soft")],
                        ),
                    )
                ],
            )
        )
        edge = _edge(graph, "Word", "Def")
        assert edge is not None
        assert edge.kind == "soft"
        # The override is on the (Word, Def) edge; there is exactly one such edge.
        assert sum(1 for e in graph.edges if e.dst == "Def") == 1

    def test_edges_to_missing_fields_are_dropped(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    (
                        "Def",
                        dict(
                            enabled=True,
                            type="text",
                            prompt="use {{Ghost}}",
                            depends_on=[FieldDep(field="AlsoGhost")],
                        ),
                    )
                ],
            )
        )
        assert graph.edges == []

    def test_matching_is_case_insensitive(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [("Def", dict(enabled=True, type="text", prompt="use {{word}}"))],
            )
        )
        edge = _edge(graph, "Word", "Def")
        assert edge is not None
        # Display name keeps the original (node) case, not the {{ref}} case.
        assert edge.src == "Word"

    def test_base_node_is_flagged(self):
        graph = FieldGraph.from_config(
            _config("Word", [("Def", dict(enabled=True, type="text"))])
        )
        by_name = {node.name: node for node in graph.nodes}
        assert by_name["Word"].is_base is True
        assert by_name["Word"].generatable is False
        assert by_name["Def"].is_base is False
        assert by_name["Def"].generatable is True

    def test_disabled_field_node_is_not_generatable(self):
        graph = FieldGraph.from_config(
            _config("Word", [("Off", dict(enabled=False, type="text"))])
        )
        by_name = {node.name: node for node in graph.nodes}
        assert by_name["Off"].generatable is False

    def test_empty_prompt_field_has_no_incoming_edge(self):
        # B3: a field with an empty prompt reads the base ONLY as a generation fallback, not as
        # a derived dependency — so it gets no base->field edge in the graph.
        graph = FieldGraph.from_config(
            _config("Word", [("Def", dict(enabled=True, type="text"))])
        )
        assert _edge(graph, "Word", "Def") is None
        assert graph.edges == []

    def test_empty_prompt_tts_field_has_no_incoming_edge(self):
        # B3 also applies to a promptless tts field: its base fallback is not a dependency.
        graph = FieldGraph.from_config(
            _config("Word", [("Audio", dict(enabled=True, type="tts"))])
        )
        assert _edge(graph, "Word", "Audio") is None
        assert graph.edges == []


class TestValidateAcyclic:
    def test_dag_passes(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        )
        graph.validate_acyclic()  # no raise

    def test_self_loop_raises(self):
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config("Word", [("A", dict(enabled=True, type="text"))])
            ).nodes,
            edges=[GraphEdge(src="A", dst="A", kind="hard", derived=True)],
        )
        with pytest.raises(SmartNotesCycleError):
            graph.validate_acyclic()

    def test_hard_two_cycle_raises(self):
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="hard", derived=True),
                GraphEdge(src="B", dst="A", kind="hard", derived=True),
            ],
        )
        with pytest.raises(SmartNotesCycleError):
            graph.validate_acyclic()

    def test_cycle_with_a_soft_edge_does_not_raise(self):
        # A hard edge A->B plus a SOFT back-edge B->A is generatable (generate A first without B's
        # optional value, then B): only the HARD subgraph must be acyclic, so this must NOT raise.
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="hard", derived=True),
                GraphEdge(src="B", dst="A", kind="soft", derived=False),
            ],
        )
        graph.validate_acyclic()  # no raise
        # ...and the soft back-edge is NOT flagged as a (hard) cycle edge for the canvas.
        assert graph.cycle_edge_keys() == set()

    def test_all_soft_two_cycle_does_not_raise(self):
        # Two fields that softly reference each other (Auto-prompt's POS <-> Definition) — valid.
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="soft", derived=False),
                GraphEdge(src="B", dst="A", kind="soft", derived=False),
            ],
        )
        graph.validate_acyclic()  # no raise
        assert graph.cycle_edge_keys() == set()

    def test_longer_cycle_raises(self):
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                        ("C", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="hard", derived=True),
                GraphEdge(src="B", dst="C", kind="hard", derived=True),
                GraphEdge(src="C", dst="A", kind="hard", derived=True),
            ],
        )
        with pytest.raises(SmartNotesCycleError):
            graph.validate_acyclic()


class TestWouldCreateCycle:
    def _chain(self):
        return FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        )

    def test_self_edge_always_cycles(self):
        assert self._chain().would_create_cycle("A", "A") is True

    def test_back_edge_closes_a_cycle(self):
        # Word -> A -> B already exists; adding B -> Word would close a cycle.
        assert self._chain().would_create_cycle("B", "Word") is True

    def test_safe_forward_edge_does_not_cycle(self):
        # Word -> B is a new forward edge; no cycle.
        assert self._chain().would_create_cycle("Word", "B") is False

    def test_case_insensitive_back_edge(self):
        assert self._chain().would_create_cycle("b", "word") is True


class TestLayeredLayout:
    def test_linear_chain_increments_column(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        ).laid_out()
        col = {node.name: node.column for node in graph.nodes}
        assert col["Word"] == 0
        assert col["A"] == 1
        assert col["B"] == 2

    def test_diamond_join_sits_after_both_branches(self):
        graph = FieldGraph.from_config(
            _config(
                "A",
                [
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                    ("C", dict(enabled=True, type="text", prompt="{{A}}")),
                    (
                        "D",
                        dict(enabled=True, type="text", prompt="{{B}} {{C}}"),
                    ),
                ],
            )
        ).laid_out()
        col = {node.name: node.column for node in graph.nodes}
        assert col["A"] == 0
        assert col["B"] == 1
        assert col["C"] == 1
        # Longest path A->B->D and A->C->D both length 2.
        assert col["D"] == 2

    def test_isolated_nodes_are_roots(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="static")),
                    ("B", dict(enabled=True, type="text", prompt="static")),
                ],
            )
        ).laid_out()
        col = {node.name: node.column for node in graph.nodes}
        assert col["Word"] == 0 and col["A"] == 0 and col["B"] == 0

    def test_rows_are_stable_within_a_column(self):
        graph = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("C", dict(enabled=True, type="text", prompt="{{Word}}")),
                ],
            )
        ).laid_out()
        rows = {node.name: node.row for node in graph.nodes if node.column == 1}
        # A, B, C share column 1 and keep config order as rows 0, 1, 2.
        assert rows == {"A": 0, "B": 1, "C": 2}

    def test_layout_is_deterministic(self):
        config = _config(
            "Word",
            [
                ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ("C", dict(enabled=True, type="text", prompt="{{A}}")),
            ],
        )
        first = FieldGraph.from_config(config).laid_out()
        second = FieldGraph.from_config(config).laid_out()
        assert [(n.name, n.column, n.row) for n in first.nodes] == [
            (n.name, n.column, n.row) for n in second.nodes
        ]

    def test_large_chain_terminates(self):
        # A 34-node chain: Word + F0..F32, each depending on the previous.
        fields = [("F0", dict(enabled=True, type="text", prompt="{{Word}}"))]
        for i in range(1, 33):
            fields.append(
                (f"F{i}", dict(enabled=True, type="text", prompt=f"{{{{F{i - 1}}}}}"))
            )
        graph = FieldGraph.from_config(_config("Word", fields)).laid_out()
        assert len(graph.nodes) == 34
        col = {node.name: node.column for node in graph.nodes}
        assert col["F32"] == 33

    def test_cycle_is_tolerated_and_flagged(self):
        # A DISPLAY layout must render a cyclic graph (the canvas is how the user breaks the
        # cycle that e.g. Auto-prompt wrote): laid_out no longer raises, every node still gets a
        # column, and both edges of the A<->B loop are reported by cycle_edge_keys to highlight.
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="hard", derived=True),
                GraphEdge(src="B", dst="A", kind="hard", derived=True),
            ],
        )
        laid = graph.laid_out()  # no raise
        assert {n.name for n in laid.nodes} == {"Word", "A", "B"}
        assert all(isinstance(n.column, int) for n in laid.nodes)
        assert graph.cycle_edge_keys() == {("a", "b"), ("b", "a")}


def _fan_out(count: int) -> SmartNotesNoteTypeConfig:
    """A base with ``count`` dependents that each {{ref}} only the base — one tall layer."""
    fields = [
        (f"D{i}", dict(enabled=True, type="text", prompt="{{Word}}"))
        for i in range(count)
    ]
    return _config("Word", fields)


class TestFlowLayout:
    def test_fan_out_wraps_into_lanes_not_one_column(self):
        # The bug this kills: 33 dependents in ONE column. They must spread across >1 lane and
        # therefore occupy more than one distinct x coordinate.
        flow = FieldGraph.from_config(_fan_out(33)).flow_layout()
        by_name = {n.name: n for n in flow.nodes}
        dependents = [by_name[f"D{i}"] for i in range(33)]
        # All dependents share the same longest-path column...
        assert {n.column for n in dependents} == {1}
        # ...but are wrapped into multiple lanes / x positions (no 33-high stack).
        assert max(n.lane for n in dependents) >= 1
        assert len({round(n.x, 3) for n in dependents}) > 1

    def test_bounds_are_positive(self):
        flow = FieldGraph.from_config(_fan_out(33)).flow_layout()
        assert flow.width > 0
        assert flow.height > 0

    def test_node_geometry_has_size(self):
        flow = FieldGraph.from_config(
            _config(
                "Word", [("Def", dict(enabled=True, type="text", prompt="{{Word}}"))]
            )
        ).flow_layout()
        node = next(n for n in flow.nodes if n.name == "Def")
        assert node.w > 0 and node.h > 0

    def test_layers_advance_in_x(self):
        flow = FieldGraph.from_config(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        ).flow_layout()
        by_name = {n.name: n for n in flow.nodes}
        assert by_name["Word"].x < by_name["A"].x < by_name["B"].x

    def test_cycle_is_tolerated(self):
        # flow_layout must also render a cyclic graph (no raise) with positive bounds + every node
        # placed, so the canvas always draws and the user can see + break the loop.
        graph = FieldGraph(
            nodes=FieldGraph.from_config(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text")),
                        ("B", dict(enabled=True, type="text")),
                    ],
                )
            ).nodes,
            edges=[
                GraphEdge(src="A", dst="B", kind="hard", derived=True),
                GraphEdge(src="B", dst="A", kind="hard", derived=True),
            ],
        )
        flow = graph.flow_layout()  # no raise
        assert {n.name for n in flow.nodes} == {"Word", "A", "B"}
        assert flow.width > 0 and flow.height > 0

    def test_empty_and_single_node_layouts_are_safe(self):
        # No div-by-zero on the degenerate shapes; bounds stay positive.
        empty = FieldGraph(nodes=[], edges=[]).flow_layout()
        assert empty.nodes == [] and empty.width > 0 and empty.height > 0
        single = FieldGraph.from_config(_config("Word", [])).flow_layout()
        assert [n.name for n in single.nodes] == ["Word"]
        assert single.width > 0 and single.height > 0

    def test_short_layer_is_centered_not_top_aligned(self):
        # The visual heart of the redesign: a 1-node base column is vertically CENTERED against
        # the tall dependent block, not pinned to the top.
        flow = FieldGraph.from_config(_fan_out(33)).flow_layout()
        by_name = {n.name: n for n in flow.nodes}
        word = by_name["Word"]
        deps = [by_name[f"D{i}"] for i in range(33)]
        top = min(n.y for n in deps)
        bottom = max(n.y + n.h for n in deps)
        word_center = word.y + word.h / 2
        assert top < word_center < bottom  # sits in the middle band
        assert word.y > top  # not top-aligned with the dependents


class TestLayeredColumnsHelper:
    def test_helper_matches_laid_out_columns(self):
        # The factored longest-path helper must yield the SAME columns laid_out() exposes.
        config = _config(
            "A",
            [
                ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ("C", dict(enabled=True, type="text", prompt="{{A}}")),
                ("D", dict(enabled=True, type="text", prompt="{{B}} {{C}}")),
            ],
        )
        graph = FieldGraph.from_config(config)
        helper = graph._layered_columns()
        laid = {n.name.strip().lower(): n.column for n in graph.laid_out().nodes}
        assert helper == laid
