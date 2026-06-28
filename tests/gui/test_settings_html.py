"""Tests for the pure settings-page HTML builder (no Qt/aqt)."""

from __future__ import annotations

from omnia.gui.settings_html import (
    PluginCardModel,
    build_settings_html,
    status_text,
)


def _card(
    plugin_id: str,
    *,
    name: str = "Name",
    description: str = "Desc",
    tooltip: str = "",
    enabled: bool = False,
    active: bool = False,
    configurable: bool = False,
) -> PluginCardModel:
    return PluginCardModel(
        id=plugin_id,
        name=name,
        description=description,
        tooltip=tooltip,
        enabled=enabled,
        active=active,
        configurable=configurable,
    )


class TestStatusText:
    def test_active_when_running(self):
        assert status_text(enabled=True, active=True) == "active"

    def test_off_when_disabled(self):
        assert status_text(enabled=False, active=False) == "off"

    def test_failed_when_enabled_but_inactive(self):
        assert "failed to enable" in status_text(enabled=True, active=False)


class TestBuildSettingsHtml:
    def test_renders_each_section_label(self):
        groups = [
            ("Reviewing", [_card("auto_flip", name="Auto Flip")]),
            ("Grading", [_card("overdue_guard", name="Overdue Guard")]),
        ]
        html = build_settings_html(groups, dark=False)
        assert "Reviewing" in html
        assert "Grading" in html
        assert "Auto Flip" in html
        assert "Overdue Guard" in html

    def test_header_and_subtitle_present(self):
        html = build_settings_html([], dark=False)
        assert "Omnia — All-in-One Toolkit" in html
        assert "changes apply immediately" in html

    def test_dark_flag_selects_dark_body_class(self):
        assert "omnia-dark" in build_settings_html([], dark=True)
        assert "omnia-light" in build_settings_html([], dark=False)

    def test_enabled_card_has_checked_switch(self):
        html = build_settings_html(
            [("Reviewing", [_card("auto_flip", enabled=True)])], dark=False
        )
        assert "checked" in html

    def test_configurable_card_has_configure_button(self):
        html = build_settings_html(
            [("AI", [_card("smart_notes", configurable=True)])], dark=False
        )
        assert '<button class="omnia-configure"' in html
        assert 'data-id="smart_notes"' in html

    def test_non_configurable_card_has_no_configure_button(self):
        # The ``.omnia-configure`` CSS rule is always defined; only the <button> element is
        # conditional, so assert on the element, not the class name.
        html = build_settings_html(
            [("Reviewing", [_card("auto_flip", configurable=False)])], dark=False
        )
        assert '<button class="omnia-configure"' not in html

    def test_tooltip_falls_back_to_description(self):
        html = build_settings_html(
            [("Grading", [_card("x", description="the desc", tooltip="")])],
            dark=False,
        )
        assert "the desc" in html

    def test_tooltip_used_when_present(self):
        html = build_settings_html(
            [("Grading", [_card("x", tooltip="cooperates with the other one")])],
            dark=False,
        )
        assert "cooperates with the other one" in html

    def test_failed_enable_marks_card(self):
        html = build_settings_html(
            [("Reviewing", [_card("x", enabled=True, active=False)])], dark=False
        )
        assert "omnia-failed" in html

    def test_html_is_escaped(self):
        html = build_settings_html(
            [("Grading", [_card("x", name="<b>hax</b>", description="a & b")])],
            dark=False,
        )
        assert "<b>hax</b>" not in html
        assert "&lt;b&gt;hax&lt;/b&gt;" in html
        assert "a &amp; b" in html

    def test_toggle_and_configure_ops_wired_in_js(self):
        html = build_settings_html([], dark=False)
        assert 'send("toggle"' in html  # toggle op posted from the switch
        assert 'send("configure"' in html  # configure op posted from the button
