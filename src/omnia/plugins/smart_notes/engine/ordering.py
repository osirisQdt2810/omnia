"""Dependency ordering for chained smart-notes field generation.

A rule's referenced fields (the ``{{Field}}`` refs in a text/image prompt, or the
``source_field`` of a TTS rule) may name another rule's ``target_field``. When they do, the
referenced rule must generate first so the dependent rule sees the freshly generated value
("chained fields"). This module builds that dependency graph and topologically orders the
rules. HARD edges are strict (a hard cycle raises :class:`SmartNotesCycleError`); SOFT edges
are optional metadata ordered best-effort and dropped when they would create a cycle, so two
fields that softly reference each other are valid and generatable.

Pure logic — no Anki imports — mirroring the reference add-on's ``dag.generate_fields_dag`` +
``has_cycle``, but as a plain topological sort over the rules a feature already selected.
Field-name matching is case-insensitive (Anki field names are user-defined).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.engine.rules import rule_prerequisites

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule


class SmartNotesCycleError(ProviderError):
    """Raised when chained field rules form a cycle (or a rule references itself)."""


def order_rules(rules: list[SmartNotesFieldRule]) -> list[SmartNotesFieldRule]:
    """Topologically order ``rules`` so each rule's dependencies generate before it.

    A rule depends on another when one of its source fields, or one of its explicit
    ``depends_on`` entries, names the other's ``target_field`` (case-insensitive). The two
    dependency kinds are treated differently, matching their semantics:

    * A **hard** edge both orders AND blocks (the dependent needs the prerequisite's value), so
      hard edges are STRICT ordering constraints. A cycle among hard edges is a real deadlock and
      raises :class:`SmartNotesCycleError`.
    * A **soft** edge is optional metadata ("use it if present"), so it orders on a BEST-EFFORT
      basis: a soft edge is honoured only when it does not create a cycle, and DROPPED otherwise
      (the dependent is generated without the not-yet-available soft value). Two fields that softly
      reference each other are therefore valid and generatable — one is simply generated first
      without the other.

    Rules whose dependencies are not in ``rules`` have no incoming edges and keep their relative
    order. Independent rules preserve input order (stable sort).

    Args:
        rules: The rules selected for one note.

    Returns:
        The same rules, reordered so dependencies precede dependents.

    Raises:
        SmartNotesCycleError: If a rule hard-references itself, or the HARD edges form a cycle.
    """
    count = len(rules)
    by_target: dict[str, int] = {
        rule.target_field.strip().lower(): index
        for index, rule in enumerate(rules)
        if rule.target_field.strip()
    }

    # Collect (prerequisite -> dependent) edges split by kind. A soft self-reference is just an
    # always-empty ref (the field isn't generated yet), so it is ignored; a hard self-reference is
    # an impossible requirement and raises.
    hard_pairs: list[tuple[int, int]] = []
    soft_pairs: list[tuple[int, int]] = []
    for index, rule in enumerate(rules):
        seen: set[int] = set()
        for field, kind in rule_prerequisites(rule):
            producer = by_target.get(field.strip().lower())
            if producer is None:
                continue  # a plain note field, not produced by another rule
            if producer == index:
                if kind == "hard":
                    raise SmartNotesCycleError(
                        f"smart_notes rule for {rule.target_field!r} references itself"
                    )
                continue  # soft self-reference — always empty, ignore for ordering
            if producer in seen:
                continue  # the same edge from both a derived ref and an explicit dep
            seen.add(producer)
            (hard_pairs if kind == "hard" else soft_pairs).append((producer, index))

    # Build the hard dependency graph and reject a real (hard) cycle up front.
    adjacency: list[set[int]] = [set() for _ in range(count)]
    for producer, dependent in hard_pairs:
        adjacency[producer].add(dependent)
    _raise_if_cyclic(adjacency, rules)

    # Add each soft edge only when it keeps the graph acyclic (best-effort metadata ordering); a
    # soft edge that would close a loop is dropped so generation still terminates.
    for producer, dependent in soft_pairs:
        if not _reaches(adjacency, dependent, producer):
            adjacency[producer].add(dependent)

    # Stable Kahn's over the resulting DAG (ready rules processed in input order).
    indegree = [0] * count
    for producer in range(count):
        for dependent in adjacency[producer]:
            indegree[dependent] += 1
    ready = sorted(index for index in range(count) if indegree[index] == 0)
    ordered: list[SmartNotesFieldRule] = []
    while ready:
        index = ready.pop(0)
        ordered.append(rules[index])
        for dependent in sorted(adjacency[index]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort()  # stable: lower input index first
    return ordered


def _reaches(adjacency: list[set[int]], start: int, target: int) -> bool:
    """Whether ``target`` is reachable from ``start`` following ``adjacency`` (start included)."""
    if start == target:
        return True
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in adjacency[node]:
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _raise_if_cyclic(
    adjacency: list[set[int]], rules: list[SmartNotesFieldRule]
) -> None:
    """Raise :class:`SmartNotesCycleError` if the (hard) ``adjacency`` graph contains a cycle."""
    count = len(adjacency)
    indegree = [0] * count
    for producer in range(count):
        for dependent in adjacency[producer]:
            indegree[dependent] += 1
    ready = [index for index in range(count) if indegree[index] == 0]
    resolved = 0
    while ready:
        node = ready.pop()
        resolved += 1
        for dependent in adjacency[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if resolved != count:
        unresolved = sorted(
            rules[i].target_field for i in range(count) if indegree[i] > 0
        )
        raise SmartNotesCycleError(
            f"smart_notes chained field rules form a cycle: {unresolved}"
        )
