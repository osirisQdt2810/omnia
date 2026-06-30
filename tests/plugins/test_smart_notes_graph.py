"""Tests for the smart-notes explicit field dependency graph (engine/graph.py).

Pure logic — no Anki. Covers ``build_field_graph`` (derived/explicit/union edges, kind
override, dropped + case-insensitive edges, base flagging), the cycle guards
(``validate_acyclic`` / ``would_create_cycle``), and ``layered_layout`` (column/row layout,
determinism, termination on a large DAG).
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
    build_field_graph,
    layered_layout,
    validate_acyclic,
    would_create_cycle,
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
        graph = build_field_graph(
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
        graph = build_field_graph(
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
        graph = build_field_graph(
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
        graph = build_field_graph(
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
        graph = build_field_graph(
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
        graph = build_field_graph(
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
        graph = build_field_graph(
            _config("Word", [("Def", dict(enabled=True, type="text"))])
        )
        by_name = {node.name: node for node in graph.nodes}
        assert by_name["Word"].is_base is True
        assert by_name["Word"].generatable is False
        assert by_name["Def"].is_base is False
        assert by_name["Def"].generatable is True

    def test_disabled_field_node_is_not_generatable(self):
        graph = build_field_graph(
            _config("Word", [("Off", dict(enabled=False, type="text"))])
        )
        by_name = {node.name: node for node in graph.nodes}
        assert by_name["Off"].generatable is False


class TestValidateAcyclic:
    def test_dag_passes(self):
        graph = build_field_graph(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        )
        validate_acyclic(graph)  # no raise

    def test_self_loop_raises(self):
        graph = FieldGraph(
            nodes=build_field_graph(
                _config("Word", [("A", dict(enabled=True, type="text"))])
            ).nodes,
            edges=[GraphEdge(src="A", dst="A", kind="hard", derived=True)],
        )
        with pytest.raises(SmartNotesCycleError):
            validate_acyclic(graph)

    def test_two_cycle_raises(self):
        graph = FieldGraph(
            nodes=build_field_graph(
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
        with pytest.raises(SmartNotesCycleError):
            validate_acyclic(graph)

    def test_longer_cycle_raises(self):
        graph = FieldGraph(
            nodes=build_field_graph(
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
            validate_acyclic(graph)


class TestWouldCreateCycle:
    def _chain(self):
        return build_field_graph(
            _config(
                "Word",
                [
                    ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                ],
            )
        )

    def test_self_edge_always_cycles(self):
        assert would_create_cycle(self._chain(), "A", "A") is True

    def test_back_edge_closes_a_cycle(self):
        # Word -> A -> B already exists; adding B -> Word would close a cycle.
        assert would_create_cycle(self._chain(), "B", "Word") is True

    def test_safe_forward_edge_does_not_cycle(self):
        # Word -> B is a new forward edge; no cycle.
        assert would_create_cycle(self._chain(), "Word", "B") is False

    def test_case_insensitive_back_edge(self):
        assert would_create_cycle(self._chain(), "b", "word") is True


class TestLayeredLayout:
    def test_linear_chain_increments_column(self):
        graph = layered_layout(
            build_field_graph(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                        ("B", dict(enabled=True, type="text", prompt="{{A}}")),
                    ],
                )
            )
        )
        col = {node.name: node.column for node in graph.nodes}
        assert col["Word"] == 0
        assert col["A"] == 1
        assert col["B"] == 2

    def test_diamond_join_sits_after_both_branches(self):
        graph = layered_layout(
            build_field_graph(
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
            )
        )
        col = {node.name: node.column for node in graph.nodes}
        assert col["A"] == 0
        assert col["B"] == 1
        assert col["C"] == 1
        # Longest path A->B->D and A->C->D both length 2.
        assert col["D"] == 2

    def test_isolated_nodes_are_roots(self):
        graph = layered_layout(
            build_field_graph(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text", prompt="static")),
                        ("B", dict(enabled=True, type="text", prompt="static")),
                    ],
                )
            )
        )
        col = {node.name: node.column for node in graph.nodes}
        assert col["Word"] == 0 and col["A"] == 0 and col["B"] == 0

    def test_rows_are_stable_within_a_column(self):
        graph = layered_layout(
            build_field_graph(
                _config(
                    "Word",
                    [
                        ("A", dict(enabled=True, type="text", prompt="{{Word}}")),
                        ("B", dict(enabled=True, type="text", prompt="{{Word}}")),
                        ("C", dict(enabled=True, type="text", prompt="{{Word}}")),
                    ],
                )
            )
        )
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
        first = layered_layout(build_field_graph(config))
        second = layered_layout(build_field_graph(config))
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
        graph = layered_layout(build_field_graph(_config("Word", fields)))
        assert len(graph.nodes) == 34
        col = {node.name: node.column for node in graph.nodes}
        assert col["F32"] == 33

    def test_cycle_raises(self):
        graph = FieldGraph(
            nodes=build_field_graph(
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
            layered_layout(graph)
