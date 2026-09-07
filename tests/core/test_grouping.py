"""Tests for the settings-UI grouping helper (``group_plugins`` / ``grouped_plugins``)."""

from __future__ import annotations

from omnia.core.manager import GROUP_ORDER, group_plugins, grouped_plugins
from omnia.core.plugin import FeaturePlugin


def _plugin(plugin_id: str, *, group: str, order: int, name: str = "") -> FeaturePlugin:
    p = FeaturePlugin()
    p.id = plugin_id
    p.group = group
    p.order = order
    p.name = name or plugin_id
    return p


class _StubManager:
    """Minimal stand-in exposing ``plugins()`` for ``grouped_plugins``."""

    def __init__(self, plugins: list[FeaturePlugin]) -> None:
        self._plugins = plugins

    def plugins(self) -> list[FeaturePlugin]:
        return self._plugins


class TestGroupPlugins:
    def test_sections_follow_preferred_order(self):
        # Shuffled on input; the output order is GROUP_ORDER's, not the input's.
        plugins = [
            _plugin("note_maintenance", group="Editing", order=60),
            _plugin("smart_notes", group="AI", order=50),
            _plugin("word_lookup", group="Integrations", order=50),
            _plugin("overdue_guard", group="Grading", order=40),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = group_plugins(plugins)
        assert [name for name, _ in groups] == list(GROUP_ORDER)

    def test_members_sorted_by_order_then_name_within_group(self):
        plugins = [
            _plugin("display_interval", group="Reviewing", order=30),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = dict(group_plugins(plugins))
        assert [p.id for p in groups["Reviewing"]] == ["auto_flip", "display_interval"]

    def test_grading_has_both_cooperating_plugins(self):
        plugins = [
            _plugin("typed_accuracy", group="Grading", order=20),
            _plugin("overdue_guard", group="Grading", order=40),
        ]
        groups = dict(group_plugins(plugins))
        assert [p.id for p in groups["Grading"]] == ["typed_accuracy", "overdue_guard"]

    def test_integrations_and_editing_are_known_groups(self):
        # Both were once unlisted and sorted with the unknowns; they are preferred now, so a
        # genuinely unknown group must fall behind them however early it was seen.
        plugins = [
            _plugin("z", group="Zeta", order=1),
            _plugin("n", group="Editing", order=1),
            _plugin("w", group="Integrations", order=1),
        ]
        groups = group_plugins(plugins)
        assert [name for name, _ in groups] == ["Integrations", "Editing", "Zeta"]

    def test_unknown_group_appended_after_known_in_first_seen_order(self):
        plugins = [
            _plugin("z", group="Zeta", order=1),
            _plugin("a", group="Alpha", order=1),
            _plugin("r", group="Reviewing", order=1),
        ]
        groups = group_plugins(plugins)
        assert [name for name, _ in groups] == ["Reviewing", "Zeta", "Alpha"]

    def test_empty_input_yields_no_sections(self):
        assert group_plugins([]) == []

    def test_grouped_plugins_delegates_to_manager(self):
        plugins = [
            _plugin("smart_notes", group="AI", order=50),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = grouped_plugins(_StubManager(plugins))
        assert [name for name, _ in groups] == ["Reviewing", "AI"]
