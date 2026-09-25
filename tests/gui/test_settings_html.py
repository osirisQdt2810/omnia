"""Tests for the pure settings-page HTML builder (no Qt/aqt)."""

from __future__ import annotations

import json
import re
import shutil

import pytest

from omnia.gui.settings_categories import CATEGORY_STYLES, DEFAULT_CATEGORY_STYLE
from omnia.gui.settings_html import (
    HEADER_ACTIONS,
    PluginCardModel,
    build_settings_html,
    category_key,
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
        # Two categories, each appearing as a tile and as the section it opens — and nothing
        # else. Omnia's own actions are not categories and are not tiles; they sit on the header
        # row, because a tile in this grid that opens a window instead of a section implies it
        # is one of the features you turn on.
        assert html.count('<button type="button" class="omnia-tile') == 2
        assert 'data-category="reviewing-0"' in html
        assert 'data-category="ai-1"' in html

    def test_omnias_own_action_sits_on_the_header_row_not_in_the_grid(self):
        html = build_settings_html([("Reviewing", [_card("auto_flip")])], dark=False)

        assert 'class="omnia-header-actions"' in html
        assert 'data-action="sync"' in html
        # Beside the title, before the grid opens — and never inside the tiles.
        assert html.index('data-action="sync"') < html.index(
            'data-category="reviewing-0"'
        )
        assert 'class="omnia-tile" data-action' not in html
        # NOT data-category: the JS pairs a category handle with the section carrying the same
        # one, so an action wearing that attribute would look for a view that does not exist.
        assert 'data-action="sync" data-category' not in html

    def test_the_script_binds_actions_by_attribute_not_by_where_they_sit(self):
        # The wiring this pins: the handler used to be bound to `.omnia-tile`, so the moment Sync
        # became a header button instead of a tile the click stopped reaching Python — a live,
        # visible control that silently did nothing, with every test still green.
        html = build_settings_html([("Reviewing", [_card("auto_flip")])], dark=False)

        assert 'querySelectorAll("[data-action]")' in html
        assert '<section class="omnia-category" data-action' not in html

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

    def test_landing_is_visible_and_everything_else_is_hidden(self):
        html = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        assert '<section id="omnia-landing" class="omnia-landing omnia-enter">' in html
        # Both category sections AND the config panel. Counted rather than named so a third
        # view added without being hidden shows up here as an off-by-one rather than as a
        # section that is simply always on screen under the landing.
        assert html.count("hidden>") == 3
        assert '<section class="omnia-config" id="omnia-config" hidden>' in html
        assert 'data-view="landing"' in html

    def test_tiles_carry_a_stagger_index(self):
        html = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        assert "--i:0;--cat-from:" in html
        assert "--i:1;--cat-from:" in html

    def test_no_plugins_still_shows_what_omnia_itself_offers(self):
        # The empty state was written when the grid was only ever plugin groups. An Omnia-level
        # feature belongs to no plugin, so "no feature plugins are installed" is no longer the
        # same statement as "there is nothing here".
        html = build_settings_html([], dark=False)

        # What Omnia itself does is still reachable...
        assert 'data-action="sync"' in html
        # ...and the grid says plainly that the rest is missing, rather than being blank.
        assert "No feature plugins are installed." in html
        # Counted on the rendered MARKUP — `data-category="` also appears inside settings.js,
        # which the page inlines, so a bare substring search can never be zero.
        assert html.count('<button type="button" class="omnia-tile') == 0
        assert html.count('<section class="omnia-category"') == 0

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

    def test_every_custom_property_used_is_one_that_is_defined(self):
        """A `var(--name)` nobody defines resolves to nothing, silently.

        Not a hypothetical: the plugin-card progress bar shipped painted with
        `linear-gradient(90deg, var(--accent-from), var(--accent-to))`, and this page's palette
        calls them `--accent` and `--accent-2`. The gradient was transparent — the bar was there,
        the width was right, the count was right, and there was nothing to see. No unit test
        could tell, because nothing was wrong with any value; only a screenshot showed it.

        Two other producers are legitimate and neither is in the sheet, so both are looked for
        where they actually are: Python writes the per-category accents into an inline `style`
        attribute, and JS sets the ones that carry a live number.
        """
        import re

        html_page = build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )
        css = _page_css(html_page)
        set_from_js = {"--progress", "--fill", "--i"}
        set_inline_by_python = set(re.findall(r"(--[a-z0-9-]+)\s*:", html_page))
        defined = (
            set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
            | set_from_js
            | set_inline_by_python
        )
        used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", css))

        assert not (used - defined), f"undefined: {sorted(used - defined)}"

    def test_no_stylesheet_rule_is_left_without_a_producer(self):
        # The flat-list markup is gone; its rules must not linger as dead weight.
        css = _page_css(build_settings_html([], dark=False))
        assert "omnia-section" not in css


class TestEveryTileIsWiredToSomething:
    """An advertised control that does nothing is worse than one that is not there."""

    @staticmethod
    def _dialog():
        """The dialog CLASS, never an instance — there is no Qt here to build one with."""
        from aqt_stubs import install_gui_stubs

        install_gui_stubs()
        from omnia.gui.settings_dialog import HANDLERS, SettingsDialog

        return HANDLERS, SettingsDialog

    def test_every_action_tile_has_a_handler_on_the_dialog(self):
        # WebDialog drops a message whose op it does not know, without a word — no dialog, no
        # error, no log line. The Sync tile shipped that way: rendered, clickable, inert.
        HANDLERS, SettingsDialog = self._dialog()

        for op, name, _icon in HEADER_ACTIONS:
            assert (
                op in HANDLERS
            ), f"the {name} tile sends {op!r} and nothing answers it"
            assert callable(getattr(SettingsDialog, HANDLERS[op], None))

    def test_the_handler_map_names_methods_that_exist(self):
        HANDLERS, SettingsDialog = self._dialog()

        for op, method in HANDLERS.items():
            assert hasattr(SettingsDialog, method), f"{op!r} names a missing {method}"


class TestTheConfigPanelIsReachable:
    """The panel is a third view of this page, so the page has to carry it and wire it."""

    @staticmethod
    def _page() -> str:
        return build_settings_html(
            [("Reviewing", [_card("a")]), ("AI", [_card("b")])], dark=False
        )

    def test_the_panel_ships_in_the_document(self):
        # Built once in the markup and filled by JS, so a new plugin needs no markup of its own.
        html = self._page()

        for handle in (
            'id="omnia-config"',
            'id="omnia-config-fields"',
            'id="omnia-config-name"',
            "omnia-config-save",
            "omnia-config-cancel",
            "omnia-config-back",
        ):
            assert handle in html, handle

    def test_the_js_renders_every_control_the_payload_can_ask_for(self):
        # Python decides which control a field gets; a name it can produce and this page cannot
        # draw is a settings row that silently is not there.
        from omnia.gui.config_panel import CONTROLS

        js = _page_js(self._page())
        for control in CONTROLS:
            assert f"{control}:" in js or f'"{control}"' in js, control

    def test_both_numeric_controls_clamp_what_they_read(self):
        """A control whose bounds are only advisory writes a value the settings model refuses.

        `min`/`max` on an `<input>` drive `:invalid` STYLING; `.value` still returns whatever
        was typed. `sliderControl` clamped from the start and `numberControl` did not, and the
        widest-bounded fields (`word_lookup.port`, `overdue_guard.force_again_after_days`) are
        exactly the ones the slider-step rule sends to the number box. Python refuses such a
        save now, but a control that cannot produce the value is the half that keeps the panel
        from arguing with the person using it.
        """
        js = _page_js(self._page())

        for control in ("sliderControl", "numberControl"):
            start = js.index("function " + control)
            body = js[start : js.index("\n  function ", start + 1)]
            assert "Math.max" in body and "Math.min" in body, (
                control + " reads its input without clamping it to the field's bounds"
            )

    def test_the_save_op_has_a_handler(self):
        from aqt_stubs import install_gui_stubs

        install_gui_stubs()
        from omnia.gui.settings_dialog import HANDLERS, SettingsDialog

        assert "save-config" in HANDLERS
        assert callable(getattr(SettingsDialog, HANDLERS["save-config"], None))

    def test_the_page_sends_exactly_the_ops_the_dialog_answers(self):
        # Both directions. An op the page sends that nothing answers is an inert control;
        # a handler nothing sends is dead weight that reads as a feature.
        import re

        from aqt_stubs import install_gui_stubs

        install_gui_stubs()
        from omnia.gui.settings_dialog import HANDLERS

        js = _page_js(self._page())
        sent = set(re.findall(r'send\(\s*"([a-z-]+)"', js))
        # `send(button.getAttribute("data-action"), …)` covers the header actions, which are
        # checked against HANDLERS by TestEveryTileIsWiredToSomething.
        assert sent, "no literal op found in the page JS"
        assert sent <= set(HANDLERS), sorted(sent - set(HANDLERS))

    def test_the_panel_back_is_not_swept_up_by_the_landing_handler(self):
        # It wears .omnia-back for the same look but must return to the CATEGORY. Binding the
        # generic handler to it would send the reader to the landing instead, losing their place.
        js = _page_js(self._page())

        assert ".omnia-back:not(.omnia-config-back)" in js


class TestTheCategoryHandleMatchesTheMarkup:
    """The handle carries a POSITION, so it has to come from what was actually rendered."""

    def test_it_matches_the_rendered_section(self):
        import re

        groups = [("Reviewing", [_card("a")]), ("AI", [_card("b")])]
        html = build_settings_html(groups, dark=False)
        rendered = [name for name, _ in groups]

        for name in rendered:
            key = category_key(name, rendered)
            assert f'data-category="{key}"' in html, (name, key)
        # And nothing else claims to be a category.
        assert set(
            re.findall(r'class="omnia-category" data-category="([^"]+)"', html)
        ) == {category_key(name, rendered) for name in rendered}

    def test_a_group_with_no_plugins_shifts_the_ones_after_it(self):
        # `group_plugins` drops an empty group, so the configured order and the rendered one are
        # different lists. Deriving the handle from the configured order would point Back at a
        # category that is not on the page — on exactly the installs where a feature is absent.
        full = ["Reviewing", "Grading", "AI"]
        without_grading = ["Reviewing", "AI"]

        assert category_key("AI", full) != category_key("AI", without_grading)
        assert category_key("AI", without_grading).endswith("-1")

    def test_an_unrendered_group_does_not_raise(self):
        # Nothing can open its panel anyway — it has no card to press.
        assert category_key("Nowhere", ["Reviewing"]) == "nowhere-1"


class TestTwoMarksOnOneAxisAreOneControl:
    """A pass mark and a "nearly perfect" mark cut the same ratio, so they are one decision.

    Two separate sliders can hold the same two numbers. What they cannot show is the
    RELATIONSHIP — three bands and where each starts — which is the whole content of the
    setting. Read as two numbers it has to be reconstructed every time the page is opened.
    """

    def _pair(self, **kw):
        from omnia.core.plugin import ConfigField

        lower = ConfigField(
            key="threshold",
            label="Pass mark",
            kind="float",
            default=0.7,
            minimum=0.0,
            maximum=1.0,
            upper_key="high_threshold",
        )
        upper = ConfigField(
            key="high_threshold",
            label="High mark",
            kind="float",
            default=0.0,
            minimum=0.0,
            maximum=1.0,
        )
        return [lower, upper]

    def _payloads(self, values):
        from omnia.gui.config_panel import _field_payloads

        return _field_payloads(self._pair(), values)

    def test_the_pair_becomes_one_range_control(self):
        (payload,) = self._payloads({"threshold": 0.8, "high_threshold": 0.95})

        assert payload["control"] == "range"
        assert payload["value"] == 0.8
        assert payload["upper"]["value"] == 0.95

    def test_the_upper_field_is_not_drawn_twice(self):
        """Two controls for one number could disagree on screen, and the user would be right
        to wonder which one Save believes."""
        payloads = self._payloads({"threshold": 0.8, "high_threshold": 0.95})

        assert [p["key"] for p in payloads] == ["threshold"]

    def test_the_page_is_told_which_second_key_to_write(self):
        (payload,) = self._payloads({"threshold": 0.8, "high_threshold": 0.95})

        assert payload["upperKey"] == "high_threshold"
        assert payload["upper"]["key"] == "high_threshold"

    def test_the_off_value_still_renders(self):
        # 0 means "no top band". The handles meet; nothing is missing from the payload.
        (payload,) = self._payloads({"threshold": 0.8, "high_threshold": 0.0})

        assert payload["upper"]["value"] == 0.0

    def test_a_dangling_partner_falls_back_to_two_sliders(self):
        """An `upper_key` naming a field that is not there — renamed, removed, mistyped.

        Falls back rather than dropping a setting: a settings page that silently stops showing
        an option is a worse failure than one that shows it plainly.
        """
        from omnia.core.plugin import ConfigField
        from omnia.gui.config_panel import _field_payloads

        lonely = ConfigField(
            key="threshold",
            label="Pass mark",
            kind="float",
            default=0.7,
            minimum=0.0,
            maximum=1.0,
            upper_key="typo_that_does_not_exist",
        )

        (payload,) = _field_payloads([lonely], {"threshold": 0.8})

        assert payload["control"] == "slider"

    def test_a_fraction_gets_a_step_fine_enough_to_place_a_threshold(self):
        """Tenths cannot express "95% correct", which is a setting people actually want."""
        (payload,) = self._payloads({"threshold": 0.8, "high_threshold": 0.95})

        assert payload["step"] == 0.05


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs a JS engine; CI runners all ship node, a contributor's box may not",
)
class TestLookingAtTheControlDoesNotChangeWhatIsStored:
    """A form must not change a setting because it was looked at.

    `sliderControl` documents this bug and its fix twenty lines above `rangeControl`: it used to
    snap stored values to the step grid on load, so a stored 1.0 landed on 1.05 and pressing
    Save to change something ELSE shifted every rate by 0.05, silently. The paired control
    reintroduced it, which is why the invariant now has a test rather than only a comment.

    Two failures it pins, neither needing the control to be touched:

    * a stored 0.72 pass mark snaps to 0.70 — the grading cutoff moves on any Save;
    * a stored (0.70, 0.72) collapses to (0.70, 0.70), which reads as OFF, so saving anything
      on that panel disables a band the user had configured.

    Off-grid values are reachable today: the sibling slider's editable readout writes typed
    values unsnapped, and these two settings are designed to arrive from sync or a different
    release.
    """

    def _read_back(self, lower: float, upper: float) -> dict:
        """What the control would hand to Save for a stored pair, having only been opened."""
        import json
        import re
        import subprocess

        import omnia.gui.settings_html as settings_html
        from omnia.gui.assets import read_asset

        source = read_asset(settings_html.__file__, "web", "settings.js")
        control = re.search(
            r"  function rangeControl\(field\) \{.*?\n  \}", source, re.S
        )
        choices = re.search(r"const SPEED_CHOICES = \[.*?\];", source, re.S)
        assert control, "rangeControl moved"

        # A DOM thin enough to run the control's constructor: it only builds elements, reads
        # their geometry on drag, and computes the two values. Nothing is dragged here.
        script = f"""
{choices.group(0) if choices else ""}
const make = () => ({{
  className: "", tabIndex: 0, style: {{setProperty(){{}}}}, textContent: "", classList: {{toggle(){{}}}},
  appendChild(){{}}, addEventListener(){{}}, setAttribute(){{}},
  getBoundingClientRect: () => ({{left: 0, width: 100}}),
}});
const el = () => make();
const document = {{createElement: make}};
const window = {{addEventListener(){{}}, removeEventListener(){{}}}};
{control.group(0).replace("  function rangeControl", "function rangeControl")}
const built = rangeControl({{
  label: "Pass", min: 0, max: 1, step: 0.05, value: {lower},
  upper: {{key: "high", label: "High", value: {upper}}},
}});
console.log(JSON.stringify({{lower: built.read(), upper: built.extra.high()}}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_an_off_grid_pass_mark_is_handed_back_unchanged(self):
        assert self._read_back(0.72, 0.0)["lower"] == 0.72

    def test_an_active_band_is_not_collapsed_into_off(self):
        """(0.70, 0.72) is an ACTIVE band — the grader gates on `high > threshold`."""
        read = self._read_back(0.70, 0.72)

        assert (
            read["upper"] == 0.72
        ), "saving anything on this panel would switch the band off"

    def test_an_on_grid_pair_still_reads_back_as_itself(self):
        read = self._read_back(0.80, 0.95)

        assert (read["lower"], read["upper"]) == (0.80, 0.95)

    def test_off_still_reads_as_off(self):
        # The upper mark at or below the lower one means no top band, and stores 0.
        assert self._read_back(0.80, 0.0)["upper"] == 0


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs a JS engine; CI runners all ship node, a contributor's box may not",
)
class TestOffStaysOffUntilTheUpperHandleIsMoved:
    """ "Off" must be something the user chose, not something that happened to them.

    It was inferred from `hi > lo`, and with the band off the upper handle parks on the lower
    one — so moving the LOWER mark down left the parked handle behind, `hi > lo` became true,
    and Save wrote a live cutoff. Nudging the pass mark by one step enabled a grading band
    nobody asked for and changed the interval every passing card gets. A round trip was worse:
    drag right and back, and the band switches on wherever you turned around.

    `high_threshold = 0` is the shipped default, so this was every user who had never touched
    the feature.
    """

    def _drive(self, lower: float, upper: float, moves: list) -> dict:
        """Open the control with a stored pair, apply `moves`, and read back what Save gets.

        Each move is ``("lo"|"hi", value)`` — the control's own `set`, which is what a drag and
        a keypress both go through.
        """
        import json
        import re
        import subprocess

        import omnia.gui.settings_html as settings_html
        from omnia.gui.assets import read_asset

        source = read_asset(settings_html.__file__, "web", "settings.js")
        control = re.search(
            r"  function rangeControl\(field\) \{.*?\n  \}", source, re.S
        )
        assert control, "rangeControl moved"

        script = f"""
const make = () => ({{
  className: "", tabIndex: 0, style: {{setProperty(){{}}}}, textContent: "", classList: {{toggle(){{}}}},
  appendChild(){{}}, addEventListener(fn, cb){{ this._on = this._on || {{}}; this._on[fn] = cb; }},
  setAttribute(){{}}, getBoundingClientRect: () => ({{left: 0, width: 100}}),
}});
const el = () => make();
const document = {{createElement: make}};
const window = {{addEventListener(){{}}, removeEventListener(){{}}}};
{control.group(0).replace("  function rangeControl", "function rangeControl")}
const built = rangeControl({{
  label: "Pass", min: 0, max: 1, step: 0.05, value: {lower},
  upper: {{key: "high", label: "High", value: {upper}}},
}});
// Reach `set` through the keyboard handler the control installed on each handle: that is the
// same function a drag calls, so this drives the real path rather than a copy of it.
const moves = {json.dumps(moves)};
for (const [which, value] of moves) {{
  built._set(which, value);
}}
console.log(JSON.stringify({{lower: built.read(), upper: built.extra.high()}}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_moving_only_the_lower_mark_does_not_switch_the_band_on(self):
        read = self._drive(0.70, 0.0, [["lo", 0.65]])

        assert (
            read["upper"] == 0
        ), "adjusting the pass mark enabled a grading band the user never asked for"

    def test_a_round_trip_on_the_lower_mark_leaves_it_off(self):
        read = self._drive(0.70, 0.0, [["lo", 0.90], ["lo", 0.70]])

        assert read["upper"] == 0

    def test_moving_the_upper_handle_is_what_turns_it_on(self):
        read = self._drive(0.70, 0.0, [["hi", 0.95]])

        assert read["upper"] == 0.95

    def test_dragging_the_upper_handle_back_down_turns_it_off_again(self):
        read = self._drive(0.70, 0.95, [["hi", 0.70]])

        assert read["upper"] == 0

    def test_an_active_band_still_follows_the_lower_mark_up(self):
        # The lower mark overtaking the upper collapses the band — deliberately, and it is the
        # one case where moving "lo" may change "hi".
        read = self._drive(0.70, 0.80, [["lo", 0.90]])

        assert read["lower"] == 0.90
        assert read["upper"] == 0


class TestTypedAccuracyDrawsItsTwoMarksTogether:
    """End to end from the real settings model, because the declaration is the load-bearing
    half: a control that renders perfectly for a hand-built pair and is never reached from the
    plugin is a feature nobody sees."""

    def _payloads(self, **values):
        from omnia.core.config.schema import schema_from_model
        from omnia.gui.config_panel import _field_payloads
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        settings = TypedAccuracySettings(**values)
        return _field_payloads(
            schema_from_model(TypedAccuracySettings), settings.dict()
        )

    def test_the_two_thresholds_share_a_track(self):
        payloads = self._payloads(threshold=0.8, high_threshold=0.95)
        by_key = {p["key"]: p for p in payloads}

        assert by_key["threshold"]["control"] == "range"
        assert by_key["threshold"]["upper"]["key"] == "high_threshold"
        assert (
            "high_threshold" not in by_key
        ), "the upper mark got a second control of its own"

    def test_the_other_options_are_untouched(self):
        keys = {p["key"] for p in self._payloads()}

        assert {"pass_ease", "fail_ease", "high_ease", "show_stats"} <= keys


class TestTheRangeControlWritesBothMarks:
    """The JS half. A control that owns two settings must register two readers, or Save keeps
    half of what the user set — and the half it drops is the one with no control of its own.
    """

    def _js(self) -> str:
        import omnia.gui.settings_html as settings_html
        from omnia.gui.assets import read_asset

        return read_asset(settings_html.__file__, "web", "settings.js")

    def test_the_control_is_registered_under_its_payload_name(self):
        assert "range: rangeControl," in self._js()

    def test_extra_readers_are_collected_on_open(self):
        js = self._js()

        assert (
            "built.extra" in js
        ), "a control owning a second setting has nowhere to register it"

    def test_the_row_forwards_extra_rather_than_swallowing_it(self):
        js = self._js()
        row = js[js.index("function fieldRow") :]
        row = row[: row.index("\n  }")]

        assert "extra: control.extra" in row

    def test_collapsing_the_handles_stores_the_off_value(self):
        """The handles meeting IS the off switch — one gesture, not a separate checkbox that
        could disagree with where the handles are."""
        js = self._js()
        control = js[js.index("function rangeControl") :]
        control = control[: control.index("\n  function ")]

        assert "hi > lo ? hi : 0" in control


_DOM_STUB = """
// A DOM thin enough to build the control and press it. Elements are real objects so `style`,
// `title` and attributes can be read back, `appendChild` records the tree, and listeners are
// kept so a press can be dispatched at one.
const NODES = [];
function make(tag, cls, text) {
  const node = {
    tag: tag || "", className: cls || "", textContent: text == null ? "" : String(text),
    style: {setProperty() {}}, attrs: {}, children: [], _on: {}, hidden: false,
    classList: {toggle() {}},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, cb) { this._on[name] = cb; },
    setAttribute(name, value) { this.attrs[name] = value; },
    // 100px over the field's 0..1 scale, so a clientX IS the value in percent.
    getBoundingClientRect: () => ({left: 0, width: 100}),
  };
  NODES.push(node);
  return node;
}
const el = (tag, cls, text) => make(tag, cls, text);
const document = {createElement: (tag) => make(tag), createTextNode: (t) => make("#text", "", t)};
const WINDOW_ON = {};
const window = {
  addEventListener(name, cb) { WINDOW_ON[name] = cb; },
  removeEventListener(name) { delete WINDOW_ON[name]; },
};
/** Every bit of text under `node`, in order. */
function textOf(node) {
  return (node.textContent || "") + node.children.map(textOf).join("");
}
"""


def _extract(*names: str) -> str:
    """Pull whole functions out of `settings.js` by name, as source.

    Running the shipped source is the point: a harness that reimplemented the control would
    have passed while the control itself was broken, which is what happened the first time.
    """
    import re

    import omnia.gui.settings_html as settings_html
    from omnia.gui.assets import read_asset

    source = read_asset(settings_html.__file__, "web", "settings.js")
    out = []
    for name in names:
        found = re.search(r"  function " + name + r"\(.*?\n  \}", source, re.S)
        assert found, f"{name} moved"
        out.append(found.group(0).replace("  function ", "function ", 1))
    return "\n".join(out)


def _run_js(script: str) -> dict:
    import json
    import subprocess

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs a JS engine; CI runners all ship node, a contributor's box may not",
)
class TestTheVisibleHandleIsThePassMark:
    """With the shipped default the two handles coincide, and a press must reach the right one.

    `high_threshold = 0` means "no top band", and the upper handle parks on the lower one — so
    every user who has never touched the feature sees ONE handle. Both are absolute siblings
    with no z-index, so the later-appended upper one painted on top and took the press.

    That made the default panel actively wrong rather than merely awkward: dragging the only
    visible handle moved the mark the user could not see. The pass mark stayed where it was,
    and the drag switched on a grading band that stages Easy for every answer above wherever
    they let go — the exact class of unasked-for grading change the two-cutoff work exists to
    put under the user's control.

    These drive `startDrag` through a press on an element, not `set` directly, because the bug
    was entirely in which element the press landed on: the suite that drove `_set` passed with
    it in place.
    """

    def _press(self, lower, upper, presses):
        """Open the control with a stored pair, press/drag at each clientX, read back Save.

        Each press is ``[down_x, ...move_x]`` in track pixels; the track is 100px wide over a
        0..1 scale, so x is the value in percent. Hit-testing mirrors the browser: the handles
        are 14px wide, a press within one takes it, later-painted wins a tie, and a handle with
        `pointer-events: none` is not a target at all.
        """
        script = _DOM_STUB + _extract("rangeControl") + f"""
const built = rangeControl({{
  label: "Pass", min: 0, max: 1, step: 0.05, value: {lower},
  upper: {{key: "high", label: "High", value: {upper}}},
}});
const handles = NODES.filter((n) => n.attrs.role === "slider");
const track = NODES.find((n) => n.children.includes(handles[0]));
const ev = (x) => ({{clientX: x, preventDefault() {{}}, stopPropagation() {{}}}});
function press(xs) {{
  const [down, ...moves] = xs;
  const hit = handles.filter(
    (h) => h.style.pointerEvents !== "none" && Math.abs(down - parseFloat(h.style.left)) <= 7
  );
  const target = hit.length ? hit[hit.length - 1] : track;
  target._on.mousedown(ev(down));
  moves.forEach((x) => WINDOW_ON.mousemove && WINDOW_ON.mousemove(ev(x)));
  WINDOW_ON.mouseup && WINDOW_ON.mouseup();
}}
{json.dumps(presses)}.forEach(press);
console.log(JSON.stringify({{lower: built.read(), upper: built.extra.high()}}));
"""
        return _run_js(script)

    def test_dragging_the_only_visible_handle_moves_the_pass_mark(self):
        """The default panel: one handle on screen, and it is the one the label names."""
        read = self._press(0.7, 0.0, [[70, 80]])

        assert (
            read["lower"] == 0.8
        ), "the pass mark did not move — the press hit the other mark"
        assert (
            read["upper"] == 0
        ), "a grading band switched on that the user never asked for"

    def test_the_pass_mark_can_be_raised_with_a_mouse(self):
        """It could only ever be LOWERED: right of the mark resolved to the upper handle, and
        the handle itself was owned by the upper one, so no rightward mouse gesture reached it.
        """
        read = self._press(0.5, 0.0, [[50, 90]])

        assert read["lower"] == 0.9

    def test_pressing_the_bare_track_to_the_right_is_what_opens_a_band(self):
        """The one gesture that turns the second cutoff on, and it needs a different target."""
        read = self._press(0.7, 0.0, [[95]])

        assert (read["lower"], read["upper"]) == (0.7, 0.95)

    def test_an_open_band_still_gives_each_handle_its_own_press(self):
        read = self._press(0.7, 0.9, [[90, 95]])

        assert (read["lower"], read["upper"]) == (0.7, 0.95)

        read = self._press(0.7, 0.9, [[70, 60]])

        assert (read["lower"], read["upper"]) == (0.6, 0.9)

    def test_closing_the_band_hands_the_press_back_to_the_pass_mark(self):
        """Drag the upper handle down onto the lower one — the band is off again, so the next
        press on the single remaining handle must move the pass mark, not reopen the band.
        """
        read = self._press(0.7, 0.9, [[90, 70], [70, 85]])

        assert (read["lower"], read["upper"]) == (0.85, 0)


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs a JS engine; CI runners all ship node, a contributor's box may not",
)
class TestBothSettingsOnThePairedRowAreExplained:
    """The upper mark is a whole setting, and the row is the only place it can be described.

    Before the two marks shared a track it had a row of its own carrying its own help — the
    only text saying what a second cutoff does, and that `0` means off. `pair_payload` still
    builds it, `rangeControl` never read it, and `fieldRow` printed the lower field's help
    alone. So it vanished at exactly the moment "off" stopped being a visible `0` and became
    a gesture nothing on screen explains.
    """

    def _row_text(self, help_lower: str, help_upper: str) -> str:
        script = (
            _DOM_STUB
            + _extract("rangeControl", "appendHelp", "helpBlock", "fieldRow")
            + f"""
const CONTROLS = {{range: rangeControl}};
const textControl = () => ({{node: el("div"), read: () => ""}});
const built = fieldRow({{
  control: "range", key: "threshold", label: "Pass", min: 0, max: 1, step: 0.05, value: 0.7,
  help: {json.dumps(help_lower)},
  upper: {{key: "high", label: "High", value: 0, help: {json.dumps(help_upper)}}},
}}, 0);
console.log(JSON.stringify({{text: textOf(built.node)}}));
"""
        )
        return _run_js(script)["text"]

    def test_the_upper_marks_help_reaches_the_row(self):
        text = self._row_text(
            "what the pass mark does", "0 = off: one cutoff, pass or fail"
        )

        assert "0 = off: one cutoff, pass or fail" in text

    def test_the_lower_fields_help_is_still_there(self):
        text = self._row_text("what the pass mark does", "0 = off")

        assert "what the pass mark does" in text

    def test_a_pair_that_declares_no_upper_help_renders_nothing_extra(self):
        text = self._row_text("what the pass mark does", "")

        assert text.count("what the pass mark does") == 1

    def test_the_real_typed_accuracy_help_is_the_text_that_shows(self):
        """Pinned against the shipped descriptor rather than a literal, so the assertion cannot
        drift away from what the plugin actually declares."""
        from omnia.gui.config_panel import _field_payloads
        from omnia.plugins.typed_accuracy import TypedAccuracyPlugin

        plugin = TypedAccuracyPlugin.__new__(TypedAccuracyPlugin)
        fields = {f.key: f for f in plugin.config_schema()}
        (payload,) = [
            p
            for p in _field_payloads(
                [fields["threshold"], fields["high_threshold"]],
                {"threshold": 0.8, "high_threshold": 0.95},
            )
            if p["control"] == "range"
        ]
        text = self._row_text(payload["help"], payload["upper"]["help"])

        first = payload["upper"]["help"].split("\n")[0]
        assert first and first in text


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs a JS engine; CI runners all ship node, a contributor's box may not",
)
class TestEachHandleAnnouncesItsScale:
    """`aria-valuenow` alone is a number a screen reader cannot place."""

    def test_both_handles_carry_the_ends_of_the_track(self):
        script = _DOM_STUB + _extract("rangeControl") + """
rangeControl({
  label: "Pass", min: 0, max: 1, step: 0.05, value: 0.7,
  upper: {key: "high", label: "High", value: 0.9},
});
const handles = NODES.filter((n) => n.attrs.role === "slider");
console.log(JSON.stringify({attrs: handles.map((h) => h.attrs)}));
"""
        for attrs in _run_js(script)["attrs"]:
            assert (attrs["aria-valuemin"], attrs["aria-valuemax"]) == ("0", "1")
            assert attrs["aria-valuenow"]


class TestOneTrackNeedsOneScale:
    """Two marks that disagree about the ends of the axis cannot share a track.

    The payload carries the LOWER field's bounds, so an upper mark declaring a wider range is
    clamped on save — the control offers part of a setting and quietly rewrites the rest. No
    such pair exists today, which is the point: this makes it impossible rather than unlikely,
    and it degrades the way a dangling `upper_key` already does, by giving each its own row.
    """

    def _fields(self, upper_max: float):
        from omnia.core.plugin import ConfigField

        return [
            ConfigField(
                key="threshold",
                label="Pass mark",
                kind="float",
                default=0.7,
                minimum=0.0,
                maximum=1.0,
                upper_key="high_threshold",
            ),
            ConfigField(
                key="high_threshold",
                label="High mark",
                kind="float",
                default=0.0,
                minimum=0.0,
                maximum=upper_max,
            ),
        ]

    def test_a_matching_pair_shares_one_track(self):
        from omnia.gui.config_panel import _field_payloads

        payloads = _field_payloads(self._fields(1.0), {})

        assert [p["control"] for p in payloads] == ["range"]

    def test_a_mismatched_pair_gets_a_row_each(self):
        from omnia.gui.config_panel import _field_payloads

        payloads = _field_payloads(self._fields(5.0), {})

        assert [p["control"] for p in payloads] == ["slider", "slider"]

    def test_the_rejected_partner_is_not_dropped(self):
        """The trap: rejecting the pairing in one place and not the other loses the setting."""
        from omnia.gui.config_panel import _field_payloads

        payloads = _field_payloads(self._fields(5.0), {})

        assert [p["key"] for p in payloads] == ["threshold", "high_threshold"]
        assert payloads[1]["max"] == 5.0, "it kept the scale it declared"
