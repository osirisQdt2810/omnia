"""Which control each declared option gets, and the payload the settings page renders from.

The per-plugin settings form is no longer a Qt dialog — it is a view of the settings page. This
module is the half of that decision worth testing: it needs no browser and no ``QApplication``,
which is the point of it living in Python rather than in ``settings.js``.

The rules it encodes are judgements, not lookups ("a choice of two is a segmented control, of
eight a dropdown"), and a judgement with no test is a preference somebody will quietly reverse.
"""

from __future__ import annotations

import json

import pytest

from omnia.core.plugin import ConfigField
from omnia.gui.config_panel import (
    CONTROLS,
    SEGMENTED_MAX,
    control_for,
    field_payload,
    panel_payload,
)


def _field(**kw) -> ConfigField:
    base = {"key": "k", "label": "Label", "kind": "text"}
    base.update(kw)
    return ConfigField(**base)


class TestWhichControl:
    def test_a_switch_for_a_boolean(self):
        assert control_for(_field(kind="bool")) == "switch"

    def test_a_short_choice_is_segmented(self):
        # Both registers of Phrase Check visible at once, and one click to pick — against a
        # dropdown's click, scan, click.
        assert (
            control_for(_field(kind="choice", choices=("spoken", "written")))
            == "segmented"
        )

    def test_exactly_the_maximum_is_still_segmented(self):
        choices = tuple(str(i) for i in range(SEGMENTED_MAX))
        assert control_for(_field(kind="choice", choices=choices)) == "segmented"

    def test_one_more_than_that_is_a_dropdown(self):
        # A segmented control of eight model ids is a wall, not a control.
        choices = tuple(str(i) for i in range(SEGMENTED_MAX + 1))
        assert control_for(_field(kind="choice", choices=choices)) == "dropdown"

    def test_a_number_with_both_ends_is_a_slider(self):
        # The bounds are the useful part of such a setting, and a spin box hides them.
        assert control_for(_field(kind="float", minimum=0.0, maximum=1.0)) == "slider"
        assert control_for(_field(kind="int", minimum=1, maximum=30)) == "slider"

    def test_a_number_with_an_open_end_is_a_plain_field(self):
        # A slider needs somewhere to stop.
        assert control_for(_field(kind="int", minimum=0)) == "number"
        assert control_for(_field(kind="float", maximum=9.0)) == "number"
        assert control_for(_field(kind="int")) == "number"

    def test_a_zero_bound_still_counts_as_a_bound(self):
        # `field.minimum or default` would read a real 0 as "unset" and widen the range past it;
        # the same falsy trap decides slider-vs-number here.
        assert control_for(_field(kind="int", minimum=0, maximum=10)) == "slider"

    def test_the_remaining_kinds_map_to_themselves(self):
        assert control_for(_field(kind="color")) == "color"
        assert control_for(_field(kind="secret")) == "secret"
        assert control_for(_field(kind="text")) == "text"

    def test_every_kind_produces_a_control_the_page_knows(self):
        # A control with no renderer draws as nothing, and a settings row that is simply absent
        # is the hardest kind of missing to notice.
        from omnia.core.plugin import FIELD_KINDS

        for kind in FIELD_KINDS:
            assert control_for(_field(kind=kind)) in CONTROLS, kind


class TestOneFieldsPayload:
    def test_it_carries_what_the_page_draws_with(self):
        payload = field_payload(
            _field(label="Explain in", help="Which language."), "English"
        )

        assert payload["key"] == "k"
        assert payload["label"] == "Explain in"
        assert payload["help"] == "Which language."
        assert payload["value"] == "English"
        assert payload["control"] == "text"

    def test_a_missing_value_is_not_a_missing_control(self):
        assert field_payload(_field(), None)["value"] == ""

    def test_a_number_arrives_as_a_number(self):
        assert (
            field_payload(_field(kind="int", minimum=0, maximum=9), "4")["value"] == 4
        )
        assert (
            field_payload(_field(kind="float", minimum=0, maximum=9), "1.5")["value"]
            == 1.5
        )

    def test_a_boolean_arrives_as_one(self):
        assert field_payload(_field(kind="bool"), 1)["value"] is True
        assert field_payload(_field(kind="bool"), None)["value"] is False

    def test_bounds_and_step_travel_with_a_number(self):
        payload = field_payload(_field(kind="float", minimum=0.25, maximum=4.0), 1.0)

        assert (payload["min"], payload["max"]) == (0.25, 4.0)
        assert payload["step"] == 0.1

    def test_an_integer_steps_by_one(self):
        # Half a card is not a thing.
        assert field_payload(_field(kind="int", minimum=1, maximum=30), 7)["step"] == 1

    def test_an_enum_member_is_normalised_to_its_value(self):
        # pydantic v1 without use_enum_values hands back the MEMBER, and a page comparing that
        # against its stringy choices matches nothing — so the first option shows as selected
        # and saving silently rewrites the setting.
        import enum

        class Colour(enum.Enum):
            GREEN = "green"

        field = _field(kind="choice", choices=("red", "green"))
        assert field_payload(field, Colour.GREEN)["value"] == "green"
        assert "green" in field_payload(field, Colour.GREEN)["choices"]

    def test_a_stored_value_outside_the_choices_is_kept(self):
        # Otherwise merely OPENING the form rewrites a hand-edited config, or a model id from a
        # newer build, to whatever happens to sort first.
        payload = field_payload(_field(kind="choice", choices=("a", "b")), "z")

        assert payload["value"] == "z"
        assert "z" in payload["choices"]

    def test_a_value_already_among_the_choices_is_not_duplicated(self):
        payload = field_payload(_field(kind="choice", choices=("a", "b")), "b")

        assert payload["choices"] == ["a", "b"]

    def test_an_empty_choice_is_not_appended_as_one(self):
        # "" means "whatever Omnia is set to" where a plugin offers it, and appending a second
        # empty row would give the dropdown a blank line nobody can tell apart from the first.
        payload = field_payload(_field(kind="choice", choices=("", "a")), "")

        assert payload["choices"] == ["", "a"]


class TestThePanelPayload:
    def _fields(self):
        return [
            _field(key="language", label="Explain in"),
            _field(key="mode", kind="choice", choices=("spoken", "written")),
            _field(key="n", kind="int", minimum=1, maximum=9),
        ]

    def test_every_field_is_present_and_in_order(self):
        payload = panel_payload(
            plugin_id="p", name="P", fields=self._fields(), values={}
        )

        assert [f["key"] for f in payload["fields"]] == ["language", "mode", "n"]

    def test_a_value_the_settings_do_not_carry_falls_back_to_the_default(self):
        fields = [_field(key="language", default="English")]

        payload = panel_payload(plugin_id="p", name="P", fields=fields, values={})

        assert payload["fields"][0]["value"] == "English"

    def test_stored_values_win_over_defaults(self):
        fields = [_field(key="language", default="English")]

        payload = panel_payload(
            plugin_id="p", name="P", fields=fields, values={"language": "Vietnamese"}
        )

        assert payload["fields"][0]["value"] == "Vietnamese"

    def test_the_id_travels_so_a_save_cannot_land_in_the_wrong_namespace(self):
        payload = panel_payload(
            plugin_id="phrase_check", name="P", fields=[], values={}
        )

        assert payload["id"] == "phrase_check"

    def test_the_category_and_accent_travel_for_back_and_for_colour(self):
        payload = panel_payload(
            plugin_id="p",
            name="P",
            fields=[],
            values={},
            category="ai-2",
            accent=("#111111", "#222222"),
        )

        assert payload["category"] == "ai-2"
        assert payload["accent"] == ["#111111", "#222222"]

    def test_no_accent_is_an_empty_list_rather_than_a_none_to_unpack(self):
        payload = panel_payload(plugin_id="p", name="P", fields=[], values={})

        assert payload["accent"] == []

    def test_a_plugin_with_no_options_still_produces_a_panel(self):
        payload = panel_payload(plugin_id="p", name="P", fields=[], values={})

        assert payload["fields"] == []

    def test_the_whole_thing_survives_json(self):
        # It crosses the pycmd bridge; a value that will not serialise is a panel that never
        # opens, reported as nothing happening when the button is pressed.
        payload = panel_payload(
            plugin_id="p", name="P", fields=self._fields(), values={"n": 3}
        )

        assert json.loads(json.dumps(payload))["fields"][2]["value"] == 3


class TestAgainstTheRealPlugins:
    """Every shipped plugin's declared options, through the real path."""

    @pytest.fixture(scope="class")
    def plugins(self):
        import omnia.plugins  # noqa: F401 - the import runs every @register
        from omnia.core.registry import FEATURE_REGISTRY

        return [cls() for cls in FEATURE_REGISTRY.values()]

    def test_each_one_renders_with_controls_the_page_knows(self, plugins):
        for plugin in plugins:
            fields = plugin.config_schema()
            if not fields:
                continue
            payload = panel_payload(
                plugin_id=plugin.id, name=plugin.name, fields=fields, values={}
            )
            assert len(payload["fields"]) == len(fields), plugin.id
            for field in payload["fields"]:
                assert field["control"] in CONTROLS, (plugin.id, field)

    def test_each_one_serialises(self, plugins):
        for plugin in plugins:
            payload = panel_payload(
                plugin_id=plugin.id,
                name=plugin.name,
                fields=plugin.config_schema(),
                values={},
            )
            json.dumps(payload)

    def test_every_numeric_field_a_slider_draws_has_both_ends(self, plugins):
        # The slider reads its track from min and max; one of them missing would put the thumb
        # at NaN, which renders as the far left and reads as "this setting is zero".
        for plugin in plugins:
            for field in plugin.config_schema():
                if control_for(field) != "slider":
                    continue
                assert field.minimum is not None, (plugin.id, field.key)
                assert field.maximum is not None, (plugin.id, field.key)
