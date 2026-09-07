"""Tests for the settings-UI grouping helper (``group_plugins`` / ``grouped_plugins``)."""

from __future__ import annotations

from omnia.core.manager import group_plugins, grouped_plugins
from omnia.core.plugin import FeaturePlugin

# The settings page supplies its own order (``gui.settings_categories.category_order``); these
# tests pass one explicitly rather than importing it, so they test the sorting mechanism and not
# whichever sections the GUI happens to ship this week — "Integrations" below is a name the grid
# no longer carries, kept here precisely because these tests are about sorting, not about it.
_ORDER = ("Reviewing", "Grading", "AI", "Integrations", "Editing")


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
            _plugin("some_integration", group="Integrations", order=50),
            _plugin("overdue_guard", group="Grading", order=40),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = group_plugins(plugins, order=_ORDER)
        assert [name for name, _ in groups] == list(_ORDER)

    def test_members_sorted_by_order_then_name_within_group(self):
        plugins = [
            _plugin("display_interval", group="Reviewing", order=30),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = dict(group_plugins(plugins, order=_ORDER))
        assert [p.id for p in groups["Reviewing"]] == ["auto_flip", "display_interval"]

    def test_grading_has_both_cooperating_plugins(self):
        plugins = [
            _plugin("typed_accuracy", group="Grading", order=20),
            _plugin("overdue_guard", group="Grading", order=40),
        ]
        groups = dict(group_plugins(plugins, order=_ORDER))
        assert [p.id for p in groups["Grading"]] == ["typed_accuracy", "overdue_guard"]

    def test_a_named_group_outranks_an_unnamed_one_seen_earlier(self):
        plugins = [
            _plugin("z", group="Zeta", order=1),
            _plugin("n", group="Editing", order=1),
            _plugin("w", group="Integrations", order=1),
        ]
        groups = group_plugins(plugins, order=_ORDER)
        assert [name for name, _ in groups] == ["Integrations", "Editing", "Zeta"]

    def test_unknown_group_appended_after_known_in_first_seen_order(self):
        plugins = [
            _plugin("z", group="Zeta", order=1),
            _plugin("a", group="Alpha", order=1),
            _plugin("r", group="Reviewing", order=1),
        ]
        groups = group_plugins(plugins, order=_ORDER)
        assert [name for name, _ in groups] == ["Reviewing", "Zeta", "Alpha"]

    def test_without_an_order_every_group_keeps_its_first_seen_position(self):
        # The default: `core` names no sections of its own, so nothing is "preferred".
        plugins = [
            _plugin("a", group="AI", order=1),
            _plugin("r", group="Reviewing", order=1),
        ]
        assert [name for name, _ in group_plugins(plugins)] == ["AI", "Reviewing"]

    def test_empty_input_yields_no_sections(self):
        assert group_plugins([], order=_ORDER) == []

    def test_grouped_plugins_delegates_to_manager(self):
        plugins = [
            _plugin("smart_notes", group="AI", order=50),
            _plugin("auto_flip", group="Reviewing", order=10),
        ]
        groups = grouped_plugins(_StubManager(plugins), order=_ORDER)
        assert [name for name, _ in groups] == ["Reviewing", "AI"]
