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
:meth:`FieldGraph.would_create_cycle` guard against cycles (over the HARD subgraph — only hard
edges deadlock; soft edges are optional metadata the generator can break);
:meth:`FieldGraph.laid_out` assigns deterministic integer ``column``/``row`` coordinates;
:meth:`FieldGraph.flow_layout` produces balanced grid-wrapped pixel geometry (a
:class:`LayoutResult` of :class:`NodeLayout`) for the interactive canvas; and
:meth:`FieldGraph.node_edge_set` returns the incoming dependency edges at one field.
"""

from __future__ import annotations

import math
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


@dataclass(frozen=True)
class NodeLayout:
    """Per-node pixel geometry for the flow renderer.

    ``x``/``y`` are the top-left corner; ``w``/``h`` the node box size. ``column`` is the
    longest-path layer (as in :meth:`FieldGraph.laid_out`) and ``lane`` is the sub-column a node
    falls into when a tall layer is grid-wrapped (0 for the first lane).
    """

    name: str
    x: float
    y: float
    w: float
    h: float
    column: int
    lane: int


@dataclass(frozen=True)
class LayoutResult:
    """The result of :meth:`FieldGraph.flow_layout`: per-node geometry + the canvas bounds.

    ``nodes`` is keyed by node order (same order as :attr:`FieldGraph.nodes`); ``width``/``height``
    are the pixel extent the renderer should frame (``fitView``).
    """

    nodes: list[NodeLayout]
    width: float
    height: float


# Flow-layout geometry (px). A node box is ~180 wide; columns are layered by longest-path depth
# and a tall column grid-WRAPS into lanes so a base with many dependents fans out instead of
# stacking into one giant column.
_NODE_W = 180.0
_NODE_H = 46.0
_COL_GAP = 96.0  # horizontal gap between layers (and between lanes within a layer)
_ROW_GAP = 26.0  # vertical gap between stacked nodes
_PAD = 40.0  # canvas padding around the whole graph
_LANE_TARGET = 8  # wrap a column into more lanes once it exceeds this many rows


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

    def _adjacency(self, *, hard_only: bool = False) -> dict[str, list[str]]:
        """Return ``src_lower -> [dst_lower, ...]``.

        With ``hard_only`` only HARD edges are included. Hard edges are the strict constraints —
        they both ORDER and BLOCK generation, so only a cycle in the hard subgraph is a real
        deadlock. A SOFT edge is optional metadata the ordering can break (generate the dependent
        without the not-yet-available soft value), so cycles that involve soft edges are still
        generatable and must NOT count as invalid — validity/cycle checks pass ``hard_only=True``.
        """
        adjacency: dict[str, list[str]] = {
            node.name.strip().lower(): [] for node in self.nodes
        }
        for edge in self.edges:
            if hard_only and edge.kind != "hard":
                continue
            adjacency.setdefault(edge.src.strip().lower(), []).append(
                edge.dst.strip().lower()
            )
        return adjacency

    def validate_acyclic(self) -> None:
        """Raise :class:`SmartNotesCycleError` if the HARD subgraph has a cycle or hard self-loop.

        Only HARD edges are considered: a hard edge both orders AND blocks, so a cycle among hard
        edges is a real deadlock (each field waits on the next). A SOFT edge is optional metadata
        the generator can break — a cycle that involves a soft edge is still generatable (drop the
        soft edge for ordering), so it is NOT an error. Two fields that softly reference each other
        (e.g. Auto-prompt's "use {{Definition}} if present" ↔ "use {{POS}} if present") are valid.
        A DAG in the hard subgraph returns ``None``.

        Raises:
            SmartNotesCycleError: If the HARD edges form any cycle (including a hard self-loop).
        """
        adjacency = self._adjacency(hard_only=True)
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
        """Return whether adding a HARD edge ``src -> dst`` would create a (hard) cycle.

        A precheck mirror of :meth:`validate_acyclic`: newly-added graph edges are hard, and only
        hard edges deadlock, so this reasons over the HARD subgraph. A self-loop (``src == dst``,
        case-insensitive) always would; otherwise the edge closes a hard cycle iff ``src`` is
        already reachable from ``dst`` along existing HARD edges. (Reaching ``src`` only via soft
        edges is fine — that cycle stays breakable.)

        Args:
            src: The prerequisite field of the proposed edge.
            dst: The dependent field of the proposed edge.

        Returns:
            ``True`` if adding the hard edge would introduce a hard cycle, ``False`` otherwise.
        """
        src_lower = src.strip().lower()
        dst_lower = dst.strip().lower()
        if src_lower == dst_lower:
            return True
        adjacency = self._adjacency(hard_only=True)
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

    def _layered_columns(self) -> dict[str, int]:
        """Return ``name_lower -> column`` by longest-path topological depth from a root.

        Roots (no incoming edge) are column 0; a node's column is one more than its deepest
        prerequisite. The single source of truth for layering — shared by :meth:`laid_out` (which
        adds the within-column row) and :meth:`flow_layout` (which adds pixel geometry). Considers
        edges of BOTH kinds and ignores edges to/from unknown fields.

        Cycle-TOLERANT: this feeds the DISPLAY layout, so a cyclic graph must still lay out — the
        canvas is precisely how the user SEES and breaks the cycle (e.g. Auto-prompt wrote two
        prompts that reference each other). A back-edge — a prerequisite still on the current DFS
        stack — is skipped for the depth, so layering always terminates and every node gets a
        column. Generation ordering (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rules`)
        and the save backstop (:func:`~omnia.gui.smart_notes.html.cycle_error_for_config` →
        :meth:`validate_acyclic`) reject cycles separately, so correctness never relies on this.

        Returns:
            ``name_lower -> column`` for every node, in no particular dict order.
        """
        order = [node.name.strip().lower() for node in self.nodes]
        rank = {name: index for index, name in enumerate(order)}

        incoming: dict[str, list[str]] = {name: [] for name in order}
        for edge in self.edges:
            dst = edge.dst.strip().lower()
            src = edge.src.strip().lower()
            if dst in incoming and src in rank:
                incoming[dst].append(src)

        column: dict[str, int] = {}
        on_stack: set[str] = set()

        def depth(name: str) -> int:
            if name in column:
                return column[name]
            on_stack.add(name)
            best = 0
            for pred in incoming.get(name, []):
                if pred in on_stack:
                    continue  # back-edge (cycle) — skip so the layering terminates
                best = max(best, 1 + depth(pred))
            on_stack.discard(name)
            column[name] = best
            return best

        for name in order:
            depth(name)
        return column

    def cycle_edge_keys(self) -> set[tuple[str, str]]:
        """Return ``(src_lower, dst_lower)`` for every HARD edge on a cycle in the HARD subgraph.

        Only a HARD cycle is a real problem (a deadlock): the display payload
        (:func:`~omnia.gui.smart_notes.html.graph_payload`) flags exactly these so the canvas
        highlights the edges the user must break, and save rejects them. A SOFT edge is optional
        metadata the ordering can break, so a soft/mixed cycle is generatable and is NOT flagged
        (two fields that softly reference each other render as ordinary green edges). A hard edge
        ``s -> d`` is on a cycle when ``d`` can already reach ``s`` along HARD edges, or it is a
        hard self-loop. Pure; reachability over the hard subgraph is cached per source.

        Returns:
            The lower-cased ``(src, dst)`` keys of every HARD edge on a hard cycle (empty when the
            hard subgraph is a DAG).
        """
        adjacency = self._adjacency(hard_only=True)
        reach: dict[str, set[str]] = {}

        def reachable(start: str) -> set[str]:
            if start in reach:
                return reach[start]
            seen: set[str] = set()
            stack = list(adjacency.get(start, []))
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(adjacency.get(node, []))
            reach[start] = seen
            return seen

        keys: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.kind != "hard":
                continue
            src = edge.src.strip().lower()
            dst = edge.dst.strip().lower()
            if src == dst or src in reachable(dst):
                keys.add((src, dst))
        return keys

    def laid_out(self) -> FieldGraph:
        """Return a copy of the graph with each node assigned a ``column`` and ``row``.

        ``column`` is the longest-path topological depth from a root (a node with no incoming
        edge): roots are column 0, and a node's column is one more than the deepest prerequisite.
        ``row`` is the node's stable index WITHIN its column, preserving config (node-list) order.
        Deterministic and integer-only; terminates on any valid DAG.

        Returns:
            A new :class:`FieldGraph` with the same nodes (coordinates set) and the same edges.

        Cycle-tolerant (a display layout): a cyclic graph still lays out — see
        :meth:`_layered_columns`.
        """
        order = [node.name.strip().lower() for node in self.nodes]
        rank = {name: index for index, name in enumerate(order)}
        column = self._layered_columns()

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

    def flow_layout(self) -> LayoutResult:
        """Compute balanced pixel geometry for the flow renderer.

        Lays the nodes out left-to-right by longest-path layer (:meth:`_layered_columns`), but a
        TALL layer is grid-WRAPPED into ``ceil(count / _LANE_TARGET)`` sub-columns (lanes) so a
        base field with many dependents fans out instead of stacking into one giant column — the
        react-flow/n8n look. Within a layer, nodes fill each lane top-to-bottom in config order;
        each layer's block is then vertically CENTERED against the tallest layer so the canvas is
        balanced. Deterministic; pixel coordinates derive from the shared integer layering.

        Returns:
            A :class:`LayoutResult` with one :class:`NodeLayout` per node (config order) and the
            canvas ``width``/``height`` the renderer frames.

        Cycle-tolerant (a display layout): a cyclic graph still lays out — see
        :meth:`_layered_columns`.
        """
        order = [node.name.strip().lower() for node in self.nodes]
        rank = {name: index for index, name in enumerate(order)}
        column = self._layered_columns()

        # Group node names per layer in config order; a layer wraps into `lanes` sub-columns.
        by_column: dict[int, list[str]] = {}
        for name in sorted(order, key=lambda n: rank[n]):
            by_column.setdefault(column[name], []).append(name)

        lanes_in: dict[int, int] = {}
        rows_per_lane: dict[int, int] = {}
        for col, names in by_column.items():
            lanes = max(1, math.ceil(len(names) / _LANE_TARGET))
            lanes_in[col] = lanes
            rows_per_lane[col] = max(1, math.ceil(len(names) / lanes))

        # The tallest layer (in rows) sets the canvas height; shorter layers center against it.
        max_rows = max((rows_per_lane[c] for c in by_column), default=1)
        block_h = max_rows * _NODE_H + (max_rows - 1) * _ROW_GAP if max_rows else 0.0

        # Left edge (x) of each layer: sum of all prior layers' lane widths + gaps.
        col_x: dict[int, float] = {}
        cursor = _PAD
        for col in sorted(by_column):
            col_x[col] = cursor
            lane_span = lanes_in[col] * _NODE_W + (lanes_in[col] - 1) * _COL_GAP
            cursor += lane_span + _COL_GAP
        total_w = cursor - _COL_GAP + _PAD if by_column else _PAD * 2

        # name_lower -> (lane, row-within-lane), filling each lane top-to-bottom.
        slot: dict[str, tuple[int, int]] = {}
        for col, names in by_column.items():
            per_lane = rows_per_lane[col]
            for index, name in enumerate(names):
                slot[name] = (index // per_lane, index % per_lane)

        layouts: list[NodeLayout] = []
        for node in self.nodes:
            key = node.name.strip().lower()
            col = column[key]
            lane, row = slot[key]
            rows_here = rows_per_lane[col]
            layer_h = rows_here * _NODE_H + (rows_here - 1) * _ROW_GAP
            top = _PAD + (block_h - layer_h) / 2.0  # vertical centering
            x = col_x[col] + lane * (_NODE_W + _COL_GAP)
            y = top + row * (_NODE_H + _ROW_GAP)
            layouts.append(
                NodeLayout(
                    name=node.name,
                    x=x,
                    y=y,
                    w=_NODE_W,
                    h=_NODE_H,
                    column=col,
                    lane=lane,
                )
            )

        total_h = block_h + _PAD * 2 if self.nodes else _PAD * 2
        return LayoutResult(nodes=layouts, width=total_w, height=total_h)

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
