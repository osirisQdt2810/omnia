"""Dependency ordering for chained smart-notes field generation.

A rule's referenced fields (the ``{{Field}}`` refs in a text/image prompt, or the
``source_field`` of a TTS rule) may name another rule's ``target_field``. When they do, the
referenced rule must generate first so the dependent rule sees the freshly generated value
("chained fields"). This module builds that dependency graph and topologically orders the
rules, raising :class:`SmartNotesCycleError` on a cycle or a self-reference.

Pure logic — no Anki imports — mirroring the reference add-on's ``dag.generate_fields_dag`` +
``has_cycle``, but as a plain topological sort over the rules a feature already selected.
Field-name matching is case-insensitive (Anki field names are user-defined).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    from omnia.core.config.models import SmartNotesFieldRule


class SmartNotesCycleError(ProviderError):
    """Raised when chained field rules form a cycle (or a rule references itself)."""


def _source_fields(rule: SmartNotesFieldRule) -> list[str]:
    """Return the field names ``rule`` reads, lower-cased (for case-insensitive matching).

    Reuses the same "what fields does this rule read" helper the skip predicate uses, so the
    dependency graph and the skip logic can never drift apart. Imported lazily to avoid a
    circular import (``logic`` imports :func:`order_rules` from this module).
    """
    from omnia.features.smart_notes.logic import _rule_source_fields

    return [name.strip().lower() for name in _rule_source_fields(rule)]


def order_rules(rules: list[SmartNotesFieldRule]) -> list[SmartNotesFieldRule]:
    """Topologically order ``rules`` so each rule's dependencies generate before it.

    A rule depends on another when one of its source fields names the other's
    ``target_field`` (case-insensitive). Rules whose dependencies are not in ``rules`` (a
    plain source field, not produced by another rule) have no incoming edges and keep their
    relative order. Independent rules preserve input order (stable sort).

    Args:
        rules: The rules selected for one note.

    Returns:
        The same rules, reordered so dependencies precede dependents.

    Raises:
        SmartNotesCycleError: If a rule references itself, or the rules form a cycle.
    """
    by_target: dict[str, int] = {
        rule.target_field.strip().lower(): index
        for index, rule in enumerate(rules)
        if rule.target_field.strip()
    }

    # dependents[i] = the rule indices that depend on rule i (edge i -> dependent).
    dependents: list[list[int]] = [[] for _ in rules]
    indegree = [0] * len(rules)
    for index, rule in enumerate(rules):
        for source in _source_fields(rule):
            producer = by_target.get(source)
            if producer is None:
                continue  # a plain note field, not produced by another rule
            if producer == index:
                raise SmartNotesCycleError(
                    f"smart_notes rule for {rule.target_field!r} references itself"
                )
            dependents[producer].append(index)
            indegree[index] += 1

    # Kahn's algorithm; process ready rules in input order to keep the output stable.
    ready = [index for index in range(len(rules)) if indegree[index] == 0]
    ordered: list[SmartNotesFieldRule] = []
    while ready:
        index = ready.pop(0)
        ordered.append(rules[index])
        for dependent in dependents[index]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort()  # stable: lower input index first

    if len(ordered) != len(rules):
        unresolved = sorted(
            rules[i].target_field for i in range(len(rules)) if indegree[i] > 0
        )
        raise SmartNotesCycleError(
            f"smart_notes chained field rules form a cycle: {unresolved}"
        )
    return ordered
