"""Tests for ``order_rule_levels`` — the parallelism view of the dependency graph.

``order_rules`` stays the sole authority on ORDER; this function only answers "what may run at
the same time". The two therefore have to agree about every edge, and the tests below pin that
agreement rather than each function's answer in isolation — a level view derived from a
slightly different edge set is the exact shape that generates a field against a blank
prerequisite with no exception, no blocked count and no failed count.
"""

from __future__ import annotations

import pytest

from omnia.plugins.smart_notes.config import FieldDep, SmartNotesFieldRule
from omnia.plugins.smart_notes.engine.ordering import (
    SmartNotesCycleError,
    _build_adjacency,
    order_rule_levels,
    order_rules,
)


def _rule(target: str, prompt: str = "", **kw) -> SmartNotesFieldRule:
    return SmartNotesFieldRule(target_field=target, prompt=prompt, **kw)


def _names(levels) -> list[list[str]]:
    return [[rule.target_field for rule in level] for level in levels]


class TestLevelsAgreeWithTheOrder:
    def test_flattened_levels_are_a_valid_topological_order_of_the_same_edges(self):
        rules = [
            _rule("Def", "define {{Word}}"),
            _rule("Usage", "use {{Def}}"),
            _rule("Pic", "draw {{Word}}", kind="image"),
            _rule("Audio", source_field="Word", kind="tts"),
        ]
        levels = order_rule_levels(rules)
        flat = [rule for level in levels for rule in level]

        assert {id(rule) for rule in flat} == {id(rule) for rule in order_rules(rules)}
        # Every rule sits strictly after everything it depends on.
        depth = {
            id(rule): index for index, level in enumerate(levels) for rule in level
        }
        adjacency = _build_adjacency(rules)
        for producer, dependents in enumerate(adjacency):
            for dependent in dependents:
                assert depth[id(rules[producer])] < depth[id(rules[dependent])]

    def test_the_golden_fixture_splits_into_the_expected_widths(self):
        rules = [
            _rule("Def", "define {{Word}}"),
            _rule("Usage", "use {{Def}}"),
            _rule("Pic", "draw {{Word}}", kind="image"),
            _rule("Audio", source_field="Word", kind="tts"),
        ]

        assert _names(order_rule_levels(rules)) == [["Def", "Pic", "Audio"], ["Usage"]]
        # …and order_rules is NOT redefined as the flattening of that: it still answers
        # [Def, Usage, Pic, Audio], which three golden tests pin.
        assert [rule.target_field for rule in order_rules(rules)] == [
            "Def",
            "Usage",
            "Pic",
            "Audio",
        ]

    def test_independent_rules_all_land_in_one_level(self):
        rules = [_rule("A", "a {{Word}}"), _rule("B", "b {{Word}}"), _rule("C")]

        assert _names(order_rule_levels(rules)) == [["A", "B", "C"]]

    def test_a_chain_is_one_rule_per_level(self):
        rules = [
            _rule("A", "a {{Word}}"),
            _rule("B", "b {{A}}"),
            _rule("C", "c {{B}}"),
        ]

        assert _names(order_rule_levels(rules)) == [["A"], ["B"], ["C"]]

    def test_no_rules_yields_no_levels(self):
        assert order_rule_levels([]) == []


class TestTheSameEdgesAreDropped:
    def test_a_soft_edge_dropped_by_order_rules_is_dropped_by_levels_too(self):
        """A soft cycle: both traversals must drop the SAME edge.

        If levels kept an edge the order dropped (or vice versa) a soft dependent could land
        beside its soft prerequisite — and nothing blocks a soft dependency, so it would
        interpolate a blank and report nothing at all.
        """
        rules = [
            _rule("A", depends_on=[FieldDep(field="B", kind="soft")]),
            _rule("B", depends_on=[FieldDep(field="A", kind="soft")]),
        ]

        levels = order_rule_levels(rules)
        # ONE edge survives (the second would close the loop), so the two rules are in
        # different levels — and both traversals agree on which way round.
        assert _names(levels) == [["B"], ["A"]]
        assert [rule.target_field for rule in order_rules(rules)] == ["B", "A"]

    def test_a_soft_edge_that_is_not_cyclic_still_orders_the_levels(self):
        rules = [
            _rule("A", depends_on=[FieldDep(field="B", kind="soft")]),
            _rule("B"),
        ]

        assert _names(order_rule_levels(rules)) == [["B"], ["A"]]


class TestCyclesRaiseFromBoth:
    def test_a_hard_cycle_raises_from_both(self):
        rules = [_rule("A", "a {{B}}"), _rule("B", "b {{A}}")]

        with pytest.raises(SmartNotesCycleError):
            order_rules(rules)
        with pytest.raises(SmartNotesCycleError):
            order_rule_levels(rules)

    def test_a_hard_self_reference_raises_from_both(self):
        rules = [_rule("A", "a {{A}}")]

        with pytest.raises(SmartNotesCycleError):
            order_rules(rules)
        with pytest.raises(SmartNotesCycleError):
            order_rule_levels(rules)


class TestLevelParallelismIsSafeForTheBlockGate:
    def test_no_rule_hard_depends_on_a_same_level_sibling(self):
        """The invariant that makes running a whole level at once safe.

        A hard prerequisite IS an edge, so its producer must be in an earlier level — which is
        why a level's rules can never need to observe each other's ``produced`` entries, and why
        the block gate can be evaluated for the whole level up front.
        """
        rules = [
            _rule("Def", "define {{Word}}"),
            _rule("Usage", "use {{Def}} and {{Word}}"),
            _rule("Pic", "draw {{Def}}", kind="image"),
            _rule("Audio", source_field="Usage", kind="tts"),
            _rule("Tag", depends_on=[FieldDep(field="Pic", kind="hard")]),
        ]
        levels = order_rule_levels(rules)
        depth = {
            rule.target_field.lower(): index
            for index, level in enumerate(levels)
            for rule in level
        }

        for level in levels:
            for rule in level:
                for name, kind in _prereqs(rule):
                    if kind != "hard" or name.lower() not in depth:
                        continue
                    assert depth[name.lower()] < depth[rule.target_field.lower()]


def _prereqs(rule):
    from omnia.plugins.smart_notes.engine.rules import rule_prerequisites

    return rule_prerequisites(rule)
