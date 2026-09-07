"""Tests for the settings-page category presentation table (no Qt/aqt)."""

from __future__ import annotations

import omnia.plugins  # noqa: F401  (runs every @register so the registry is populated)
from omnia.core.plugin import FeaturePlugin
from omnia.core.registry import FEATURE_REGISTRY
from omnia.gui.settings_categories import (
    CATEGORY_STYLES,
    DEFAULT_CATEGORY_STYLE,
    category_order,
    category_style,
)


class TestCategoryStyles:
    def test_the_order_is_the_table(self):
        # One table, so a category cannot be ordered without being painted. If these ever
        # became two lists again, this is the test that would have to be deleted to allow it.
        assert category_order() == tuple(CATEGORY_STYLES)

    def test_the_base_class_default_group_is_styled(self):
        # CLAUDE.md's "adding a plugin" steps say to set id/name/description and never mention
        # `group`, so the documented happy path lands on FeaturePlugin's default. It has to
        # render like any other category rather than fail a test the author never touched.
        assert FeaturePlugin.group in CATEGORY_STYLES

    def test_every_plugin_group_has_a_style(self):
        # A plugin may declare any group; an unlisted one still renders (via the default), but
        # a group we actually ship should have been given real art.
        groups = {plugin.group for plugin in FEATURE_REGISTRY.values()}
        assert groups - set(CATEGORY_STYLES) == set()

    def test_unknown_group_returns_default_style(self):
        assert category_style("Sync From Space") is DEFAULT_CATEGORY_STYLE

    def test_known_group_returns_its_own_style(self):
        assert category_style("AI") is CATEGORY_STYLES["AI"]

    def test_default_style_is_complete(self):
        style = DEFAULT_CATEGORY_STYLE
        assert style.icon.strip()
        assert style.blurb.strip()
        assert style.accent_from.strip()
        assert style.accent_to.strip()

    def test_default_accents_track_the_theme(self):
        # A literal colour here would look right in one palette and wrong in the other; the
        # default must borrow whichever accent the active theme defines.
        assert DEFAULT_CATEGORY_STYLE.accent_from.startswith("var(--")
        assert DEFAULT_CATEGORY_STYLE.accent_to.startswith("var(--")

    def test_every_style_is_complete(self):
        for name, style in CATEGORY_STYLES.items():
            assert style.icon.strip(), name
            assert style.blurb.strip(), name
            assert style.accent_from.strip(), name
            assert style.accent_to.strip(), name

    def test_styles_name_no_feature(self):
        # ADR-011/012: shared settings UI describes a CATEGORY, never a particular feature.
        # A blurb that names one goes stale the moment that plugin moves group or is removed.
        ids = set(FEATURE_REGISTRY)
        names = {plugin_id.replace("_", " ") for plugin_id in ids}
        styles = {**CATEGORY_STYLES, "<default>": DEFAULT_CATEGORY_STYLE}
        for name, style in styles.items():
            blurb = style.blurb.lower()
            assert not [term for term in ids | names if term in blurb], name
