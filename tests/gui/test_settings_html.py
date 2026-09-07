"""Tests for the pure settings-page HTML builder (no Qt/aqt)."""

from __future__ import annotations

import re

from omnia.gui.settings_categories import CATEGORY_STYLES, DEFAULT_CATEGORY_STYLE
from omnia.gui.settings_html import (
    PluginCardModel,
    build_settings_html,
    category_models,
    status_text,
)

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


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


def _page_js(html: str) -> str:
    """The page's inlined script ONLY.

    Searching the whole document for a JS snippet is how an assertion becomes theatre: the
    stylesheet contains the same selector strings (``.omnia-tile``, ``.omnia-back``,
    ``.omnia-switch input:checked``), so those substrings are present whether or not any
    script is.
    """
    return html.split("<script>", 1)[1].split("</script>", 1)[0]


def _css_rule(css: str, selector: str) -> str:
    """The declarations of the ONE rule whose selector list starts with ``selector``."""
    match = re.search(re.escape(selector) + r"[^{,]*\{([^}]*)\}", css)
    assert match, f"no rule found for {selector}"
    return match.group(1)


def _page_css(html: str) -> str:
    """The page's inlined stylesheet, with comments stripped.

    The comments explain the very rules the assertions below pin (why the fill mode is not
    forwards, why [hidden] is spelled out), so a naive substring search would match the
    explanation instead of the code.
    """
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    return _CSS_COMMENT.sub("", css)


class TestStatusText:
    def test_active_when_running(self):
        assert status_text(enabled=True, active=True) == "active"

    def test_off_when_disabled(self):
        assert status_text(enabled=False, active=False) == "off"

    def test_failed_when_enabled_but_inactive(self):
        assert "failed to enable" in status_text(enabled=True, active=False)


class TestCategoryModels:
    def test_group_order_is_preserved(self):
        groups = [("Reviewing", [_card("a")]), ("AI", [_card("b")])]
        assert [c.name for c in category_models(groups)] == ["Reviewing", "AI"]

    def test_keys_are_dom_safe_and_unique_for_colliding_names(self):
        groups = [("My Stuff", [_card("a")]), ("My-Stuff", [_card("b")])]
        keys = [c.key for c in category_models(groups)]
        assert keys == ["my-stuff-0", "my-stuff-1"]
        assert all(re.fullmatch(r"[a-z0-9-]+", key) for key in keys)

    def test_key_survives_a_name_with_no_usable_characters(self):
        assert category_models([("…", [])])[0].key == "group-0"

    def test_counts_come_from_the_cards(self):
        cards = [_card("a", enabled=True), _card("b"), _card("c", enabled=True)]
        model = category_models([("Reviewing", cards)])[0]
        assert (model.on_count, model.total) == (2, 3)

    def test_a_failed_enable_still_counts_as_on(self):
        # It renders CHECKED, and the JS recounts from the checked inputs — seeding from
        # `enabled` keeps both sides telling the same story.
        model = category_models([("AI", [_card("a", enabled=True, active=False)])])[0]
        assert model.on_count == 1

    def test_known_group_gets_its_style_and_unknown_gets_the_default(self):
        models = category_models([("AI", []), ("Sync", [])])
        assert models[0].style is CATEGORY_STYLES["AI"]
        assert models[1].style is DEFAULT_CATEGORY_STYLE

    def test_empty_input_yields_no_categories(self):
        assert category_models([]) == []


class TestLandingView:
    def test_one_tile_per_category(self):
        html = build_settings_html(
            [("Reviewing", [_card("auto_flip")]), ("AI", [_card("smart_notes")])],
            dark=False,
        )
        assert html.count('<button type="button" class="omnia-tile') == 2
        assert 'data-category="reviewing-0"' in html
        assert 'data-category="ai-1"' in html

    def test_tile_carries_the_name_blurb_and_counts(self):
        html = build_settings_html(
            [("AI", [_card("a", enabled=True), _card("b")])], dark=False
        )
        assert ">AI</span>" in html
        assert CATEGORY_STYLES["AI"].blurb in html
        assert '<b class="omnia-tile-on">1</b> of 2 on' in html
        assert 'data-total="2"' in html

    def test_tile_with_something_on_is_marked(self):
        on = build_settings_html([("AI", [_card("a", enabled=True)])], dark=False)
        off = build_settings_html([("AI", [_card("a")])], dark=False)
        assert '<button type="button" class="omnia-tile omnia-on"' in on
        assert '<button type="button" class="omnia-tile"' in off

    def test_landing_is_visible_and_every_category_is_hidden(self):
        html = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        assert '<section id="omnia-landing" class="omnia-landing omnia-enter">' in html
        assert html.count("hidden>") == 2  # both category sections, and nothing else
        assert 'data-view="landing"' in html

    def test_tiles_carry_a_stagger_index(self):
        html = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        assert "--i:0;--cat-from:" in html
        assert "--i:1;--cat-from:" in html

    def test_empty_state_when_there_are_no_plugins(self):
        html = build_settings_html([], dark=False)
        assert "No feature plugins are installed." in html
        assert 'class="omnia-tile' not in html
        assert 'class="omnia-category"' not in html

    def test_data_total_matches_the_switches_the_view_renders(self):
        # refreshCount divides the live checked count by this attribute; if they ever disagree
        # the tile reads "3 of 2 on" forever and nothing else notices.
        html = build_settings_html(
            [("AI", [_card("a"), _card("b"), _card("c")])], dark=False
        )
        assert 'data-total="3"' in html
        assert html.count('<input type="checkbox"') == 3

    def test_an_unlisted_group_renders_its_theme_variable_accents_intact(self):
        # The default style's accents are var() references, the only values that go through
        # html.escape and still have to survive as CSS.
        html = build_settings_html([("Sync", [_card("a")])], dark=False)
        assert "--cat-from:var(--accent);--cat-to:var(--accent-2)" in html

    def test_colliding_group_names_stay_paired_end_to_end(self):
        html = build_settings_html(
            [("My Stuff", [_card("a")]), ("My-Stuff", [_card("b")])], dark=False
        )
        assert html.count('data-category="my-stuff-0"') == 2
        assert html.count('data-category="my-stuff-1"') == 2

    def test_group_name_is_escaped_in_the_tile(self):
        html = build_settings_html([("<b>AI</b>", [_card("a")])], dark=False)
        assert "<b>AI</b>" not in html
        assert "&lt;b&gt;AI&lt;/b&gt;" in html


class TestCategoryView:
    def test_each_category_gets_a_view_paired_to_its_tile(self):
        html = build_settings_html(
            [("Reviewing", [_card("auto_flip")]), ("AI", [_card("smart_notes")])],
            dark=False,
        )
        assert html.count('<section class="omnia-category"') == 2
        for key in ("reviewing-0", "ai-1"):
            assert html.count(f'data-category="{key}"') == 2  # the tile and its view

    def test_every_view_has_a_back_button_and_a_heading(self):
        html = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        assert html.count('<button type="button" class="omnia-back">') == 2
        # The heading is the focus target when the view opens, so it must stay focusable.
        assert html.count('<h2 class="omnia-cat-name" tabindex="-1">') == 2

    def test_all_cards_of_all_categories_are_rendered(self):
        html = build_settings_html(
            [
                ("Reviewing", [_card("auto_flip", name="Auto Flip")]),
                ("AI", [_card("smart_notes", name="Smart Notes")]),
            ],
            dark=False,
        )
        assert "Auto Flip" in html
        assert "Smart Notes" in html

    def test_cards_carry_a_stagger_index(self):
        html = build_settings_html([("AI", [_card("a"), _card("b")])], dark=False)
        assert 'style="--i:0"' in html
        assert 'style="--i:1"' in html

    def test_view_carries_the_gradient_but_no_stagger_of_its_own(self):
        # The section enters as a whole; only its cards stagger.
        html = build_settings_html([("AI", [_card("a")])], dark=False)
        assert f'style="--cat-from:{CATEGORY_STYLES["AI"].accent_from}' in html

    def test_every_group_and_plugin_appears_in_the_document(self):
        groups = [
            ("Reviewing", [_card("auto_flip", name="Auto Flip")]),
            ("Grading", [_card("overdue_guard", name="Overdue Guard")]),
        ]
        html = build_settings_html(groups, dark=False)
        assert "Reviewing" in html
        assert "Grading" in html
        assert "Auto Flip" in html
        assert "Overdue Guard" in html


class TestCards:
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
        assert '<input type="checkbox" data-id="auto_flip" checked>' in html

    def test_disabled_card_has_an_unchecked_switch(self):
        html = build_settings_html([("Reviewing", [_card("auto_flip")])], dark=False)
        assert '<input type="checkbox" data-id="auto_flip">' in html

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

    def test_no_info_popover_without_tooltip(self):
        # No extended tooltip → description shown inline, no (i) popover (nothing to add).
        html = build_settings_html(
            [("Grading", [_card("x", description="the desc", tooltip="")])],
            dark=False,
        )
        assert "the desc" in html
        assert 'class="omnia-info"' not in html  # markup, not the CSS rule

    def test_info_popover_rendered_when_tooltip_present(self):
        html = build_settings_html(
            [("Grading", [_card("x", tooltip="cooperates with the other one")])],
            dark=False,
        )
        assert "cooperates with the other one" in html
        assert 'class="omnia-info"' in html
        assert 'class="omnia-tip"' in html
        # Nothing on the page may carry a raw browser title= attribute — not the cards, and
        # not the tiles, back buttons or icons the redesign added.
        assert 'title="' not in html

    def test_info_popover_suppressed_when_tooltip_equals_description(self):
        # A tooltip that just repeats the visible description adds nothing → no popover.
        html = build_settings_html(
            [("Grading", [_card("x", description="same text", tooltip="same text")])],
            dark=False,
        )
        assert 'class="omnia-info"' not in html

    def test_tooltip_newlines_become_line_breaks(self):
        html = build_settings_html(
            [("Grading", [_card("x", tooltip="line one\nline two")])],
            dark=False,
        )
        assert "line one<br>line two" in html

    def test_failed_enable_marks_card(self):
        html = build_settings_html(
            [("Reviewing", [_card("x", enabled=True, active=False)])], dark=False
        )
        assert '<div class="omnia-card omnia-failed"' in html

    def test_html_is_escaped(self):
        html = build_settings_html(
            [("Grading", [_card("x", name="<b>hax</b>", description="a & b")])],
            dark=False,
        )
        assert "<b>hax</b>" not in html
        assert "&lt;b&gt;hax&lt;/b&gt;" in html
        assert "a &amp; b" in html


class TestPageAssets:
    """Weak string assertions on the inlined JS/CSS.

    They cannot prove the page behaves — that is what the CDP run in the PR does — but they
    are the only headless guard for the handful of rules that are easy to break silently and
    expensive to notice, so each one pins a rule with a comment saying which. Every assertion
    is scoped to the asset that is supposed to carry it (:func:`_page_js` / :func:`_page_css`);
    searching the whole document instead lets the stylesheet satisfy a claim about the script.
    """

    def test_toggle_and_configure_ops_wired_in_js(self):
        js = _page_js(build_settings_html([], dark=False))
        assert 'send("toggle"' in js  # toggle op posted from the switch
        assert 'send("configure"' in js  # configure op posted from the button

    def test_view_switching_is_wired_in_js(self):
        js = _page_js(build_settings_html([], dark=False))
        assert ".omnia-tile" in js
        assert ".omnia-back" in js
        assert '"Escape"' in js

    def test_js_recounts_from_the_checked_switches(self):
        # A cached number would drift the moment an enable failed and bounced back off.
        assert ".omnia-switch input:checked" in _page_js(
            build_settings_html([], dark=False)
        )

    def test_js_writes_only_the_number_not_the_sentence(self):
        # The wording is Python's, rendered once; JS that rebuilt the sentence could drift.
        js = _page_js(build_settings_html([], dark=False))
        assert ".omnia-tile-on" in js
        assert '" of "' not in js

    def test_configure_pushes_the_new_card_state_back_in(self):
        # A reload can fail, and there is no re-render to lean on; Qt calls this.
        assert "setCardState" in _page_js(build_settings_html([], dark=False))

    def test_hidden_views_are_explicitly_display_none(self):
        css = _page_css(build_settings_html([], dark=False))
        assert re.search(r"\[hidden\][^{]*\{\s*display:\s*none", css)

    def test_every_entrance_rule_fills_backwards(self):
        # Asserted PER RULE. "backwards appears in the stylesheet" passes even when the one
        # rule that matters — the card stagger — has been switched to `both`, which is exactly
        # the change that kills the hover lift.
        css = _page_css(build_settings_html([], dark=False))
        assert "@keyframes" in css
        for selector in (
            ".omnia-landing.omnia-enter",
            ".omnia-category.omnia-enter",
            ".omnia-category.omnia-enter .omnia-card",
        ):
            rule = _css_rule(css, selector)
            assert "backwards" in rule, selector
            assert "forwards" not in rule, selector
            assert " both" not in rule, selector

    def test_reduced_motion_is_honoured(self):
        css = _page_css(build_settings_html([], dark=False))
        assert "prefers-reduced-motion" in css

    def test_css_stays_within_the_qtwebengine_floor(self):
        # Anki ships Qt 6.6 on some platforms; these land as no-ops there and would silently
        # break the layout or the palette.
        css = _page_css(build_settings_html([], dark=False))
        for feature in ("color-mix(", ":has(", "@container", "@property"):
            assert feature not in css

    def test_no_stylesheet_rule_is_left_without_a_producer(self):
        # The flat-list markup is gone; its rules must not linger as dead weight.
        css = _page_css(build_settings_html([], dark=False))
        assert "omnia-section" not in css
