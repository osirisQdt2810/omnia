"""The explicit field dependency graph for a smart-notes note type.

Pure logic — no Anki imports. Smart-notes derives an implicit field DAG from each field's
prompt ``{{refs}}`` (and a TTS rule's source field) and topologically orders generation. This
module makes that graph EXPLICIT and adds hard/soft semantics:

* The effective graph's edges are the DERIVED edges (from ``{{refs}}`` / source fields,
  default kind ``"hard"``) UNIONed with each field's explicit ``depends_on`` entries. An
  explicit entry for the same ``(src, dst)`` OVERRIDES the derived kind (e.g. recolours a
  derived hard edge to soft); an explicit-only edge is added with ``derived=False``.
* Field-name matching is case-insensitive (Anki field names are user-defined); display names
  keep their original case. Edges referencing a field not present in the note type are dropped.

:meth:`FieldGraph.from_config` constructs the graph; :meth:`FieldGraph.validate_acyclic` /
:meth:`FieldGraph.would_create_cycle` guard against cycles (over edges of BOTH kinds);
:meth:`FieldGraph.laid_out` assigns deterministic ``column``/``row`` coordinates for a renderer;
and :meth:`FieldGraph.node_edge_set` returns the incoming dependency edges at one field.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from omnia.plugins.smart_notes.engine.ordering import SmartNotesCycleError
from omnia.plugins.smart_notes.engine.rules import (
    compile_field_rule,
    rule_prerequisites,
    rule_source_fields,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldConfig,
        SmartNotesNoteTypeConfig,
    )
    from omnia.plugins.smart_notes.engine.consistency import NodeEdgeSet


@dataclass(frozen=True)
class GraphEdge:
    """A dependency edge ``src -> dst`` (``src`` is the prerequisite, ``dst`` the dependent).

    ``kind`` is ``"hard"`` (order + block) or ``"soft"`` (order only). ``derived`` is True when
    the edge came from a prompt ``{{ref}}`` / source field, False when it exists only because a
    field listed it in ``depends_on``.
    """

    src: str
    dst: str
    kind: str
    derived: bool


@dataclass(frozen=True)
class FieldNode:
    """One field in the note type. ``is_base`` marks the input field (never generated).

    ``generatable`` is True for an enabled, non-base field (one that produces content).
    ``column``/``row`` are layout coordinates filled in by :meth:`FieldGraph.laid_out` (0 until
    then).
    """

    name: str
    is_base: bool
    generatable: bool
    column: int = 0
    row: int = 0


def _field_is_generatable(field: SmartNotesFieldConfig, base_lower: str) -> bool:
    """Whether ``field`` is an enabled, non-base field (i.e. it generates content)."""
    return field.enabled and field.field.strip().lower() != base_lower


@dataclass(frozen=True)
class FieldGraph:
    """The effective dependency graph: the note type's field nodes + their edges.

    Owns the graph behaviour: :meth:`from_config` builds it from a note-type config,
    :meth:`validate_acyclic` / :meth:`would_create_cycle` guard cycles, :meth:`laid_out` returns
    a layered copy with coordinates, and :meth:`node_edge_set` returns the incoming edges at a
    field. Frozen — the layout methods return a new graph rather than mutating in place.
    """

    nodes: list[FieldNode]
    edges: list[GraphEdge]

    @classmethod
    def from_config(cls, config: SmartNotesNoteTypeConfig) -> FieldGraph:
        """Build the effective field dependency graph for ``config``.

        Nodes are the base field plus every field in ``config.fields``. Edges are the DERIVED
        edges — each field's prompt ``{{refs}}`` / source field (the same "source fields" notion
        ordering uses), default kind ``"hard"`` — UNIONed with each field's explicit
        ``depends_on`` entries. An explicit entry for the same ``(src, dst)`` overrides the
        derived kind; an explicit-only edge is added with ``derived=False``. Matching is
        case-insensitive and edges whose ``src`` or ``dst`` is not a known field are dropped. No
        layout is computed here.

        Args:
            config: The note type's smart-notes config.

        Returns:
            The effective :class:`FieldGraph` (nodes + deduped edges, no coordinates).
        """
        base = config.base_field.strip()
        base_lower = base.lower()
        nodes: list[FieldNode] = []
        # display[lower-name] -> the original-case display name (first occurrence wins).
        display: dict[str, str] = {}
        if base:
            display[base_lower] = base
            nodes.append(FieldNode(name=base, is_base=True, generatable=False))
        for field in config.fields:
            name = field.field.strip()
            lower = name.lower()
            if not name or lower in display:
                continue
            display[lower] = name
            nodes.append(
                FieldNode(
                    name=name,
                    is_base=lower == base_lower,
                    generatable=_field_is_generatable(field, base_lower),
                )
            )

        # (src_lower, dst_lower) -> GraphEdge, so an explicit dep can override a derived edge's
        # kind and duplicate edges collapse. Derived edges are added first, then explicit.
        edges: dict[tuple[str, str], GraphEdge] = {}

        def add_edge(
            src_lower: str, dst_lower: str, kind: str, *, derived: bool
        ) -> None:
            if src_lower not in display or dst_lower not in display:
                return  # edge references a field not present in the note type — drop it
            edges[src_lower, dst_lower] = GraphEdge(
                src=display[src_lower],
                dst=display[dst_lower],
                kind=kind,
                derived=derived,
            )

        for field in config.fields:
            dst_lower = field.field.strip().lower()
            if not dst_lower:
                continue
            # Build the same rule the engine compiles so the graph reads dependencies through
            # the single source of truth (rule_prerequisites); the graph only adds ``derived``.
            rule = compile_field_rule(field, base)
            derived_sources = {
                name.strip().lower() for name in rule_source_fields(rule)
            }
            for prereq, kind in rule_prerequisites(rule):
                src_lower = prereq.strip().lower()
                add_edge(
                    src_lower, dst_lower, kind, derived=src_lower in derived_sources
                )

        return cls(nodes=nodes, edges=list(edges.values()))

    def _adjacency(self) -> dict[str, list[str]]:
        """Return ``src_lower -> [dst_lower, ...]`` over edges of BOTH kinds."""
        adjacency: dict[str, list[str]] = {
            node.name.strip().lower(): [] for node in self.nodes
        }
        for edge in self.edges:
            adjacency.setdefault(edge.src.strip().lower(), []).append(
                edge.dst.strip().lower()
            )
        return adjacency

    def validate_acyclic(self) -> None:
        """Raise :class:`SmartNotesCycleError` if the graph contains a cycle or self-loop.

        Considers edges of BOTH kinds (hard and soft both order generation, so either can form a
        cycle). A DAG returns ``None``.

        Raises:
            SmartNotesCycleError: If any cycle (including a self-loop) exists.
        """
        adjacency = self._adjacency()
        # 0 = unvisited, 1 = on the current DFS stack, 2 = fully explored.
        state: dict[str, int] = {name: 0 for name in adjacency}

        def visit(node: str) -> None:
            state[node] = 1
            for nxt in adjacency.get(node, []):
                if state.get(nxt) == 1:
                    raise SmartNotesCycleError(
                        f"smart_notes field dependencies form a cycle at {nxt!r}"
                    )
                if state.get(nxt) == 0:
                    visit(nxt)
            state[node] = 2

        for name in adjacency:
            if state[name] == 0:
                visit(name)

    def would_create_cycle(self, src: str, dst: str) -> bool:
        """Return whether adding edge ``src -> dst`` to the graph would create a cycle.

        A client-side precheck mirror of :meth:`validate_acyclic`: a self-loop (``src == dst``,
        case-insensitive) always would; otherwise the edge closes a cycle iff ``src`` is already
        reachable from ``dst`` along existing edges of either kind.

        Args:
            src: The prerequisite field of the proposed edge.
            dst: The dependent field of the proposed edge.

        Returns:
            ``True`` if adding the edge would introduce a cycle, ``False`` otherwise.
        """
        src_lower = src.strip().lower()
        dst_lower = dst.strip().lower()
        if src_lower == dst_lower:
            return True
        adjacency = self._adjacency()
        stack = [dst_lower]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == src_lower:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, []))
        return False

    def laid_out(self) -> FieldGraph:
        """Return a copy of the graph with each node assigned a ``column`` and ``row``.

        ``column`` is the longest-path topological depth from a root (a node with no incoming
        edge): roots are column 0, and a node's column is one more than the deepest prerequisite.
        ``row`` is the node's stable index WITHIN its column, preserving config (node-list) order.
        Deterministic and integer-only; terminates on any valid DAG.

        Returns:
            A new :class:`FieldGraph` with the same nodes (coordinates set) and the same edges.

        Raises:
            SmartNotesCycleError: If the graph contains a cycle (layering cannot terminate).
        """
        self.validate_acyclic()
        order = [node.name.strip().lower() for node in self.nodes]
        rank = {name: index for index, name in enumerate(order)}

        incoming: dict[str, list[str]] = {name: [] for name in order}
        for edge in self.edges:
            dst = edge.dst.strip().lower()
            src = edge.src.strip().lower()
            if dst in incoming and src in rank:
                incoming[dst].append(src)

        column: dict[str, int] = {}

        def depth(name: str) -> int:
            if name in column:
                return column[name]
            preds = incoming.get(name, [])
            column[name] = 0 if not preds else 1 + max(depth(pred) for pred in preds)
            return column[name]

        for name in order:
            depth(name)

        # Stable within-column row: nodes in a column keep their config order.
        by_column: dict[int, list[str]] = {}
        for name in sorted(order, key=lambda n: rank[n]):
            by_column.setdefault(column[name], []).append(name)
        row: dict[str, int] = {}
        for names in by_column.values():
            for index, name in enumerate(names):
                row[name] = index

        placed = [
            replace(
                node,
                column=column[node.name.strip().lower()],
                row=row[node.name.strip().lower()],
            )
            for node in self.nodes
        ]
        return FieldGraph(nodes=placed, edges=list(self.edges))

    def node_edge_set(self, target_field: str) -> NodeEdgeSet:
        """Return the incoming dependency edges at ``target_field`` in this graph.

        Reads the field's incoming edges straight from this graph's already-computed ``edges``
        (the derived ``{{refs}}`` UNIONed with explicit ``depends_on``, kind-overrides applied,
        edges to unknown fields already dropped at build time). A self-reference is excluded and
        a target with no incoming edges (or not in the graph) yields an empty set.

        Args:
            target_field: The field whose incoming edges to read.

        Returns:
            A :class:`~omnia.plugins.smart_notes.engine.consistency.NodeEdgeSet` for the field.
        """
        from omnia.plugins.smart_notes.engine.consistency import NodeEdgeSet

        target_lower = target_field.strip().lower()
        edges = frozenset(
            (edge.src.strip().lower(), edge.kind)
            for edge in self.edges
            if edge.dst.strip().lower() == target_lower
            and edge.src.strip().lower() != target_lower
        )
        return NodeEdgeSet(target=target_field, edges=edges)
