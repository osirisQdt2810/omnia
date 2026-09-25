"""Tests for the pure settings-page HTML builder (no Qt/aqt)."""

from __future__ import annotations

import re

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
