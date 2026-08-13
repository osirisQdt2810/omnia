"""Tests for the save-time merge policy: what a settings save keeps, and what it may replace.

Pure — the classes under test carry no Qt, which is the point of their living in the plugin
rather than in the dialog that composes them.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from omnia.plugins.note_maintenance.base import TaskConfigBase
from omnia.plugins.note_maintenance.settings_merge import TaskOptions, TaskSectionMerge


class _DemoConfig(TaskConfigBase):
    """A task config with one option of each kind the panel has to deal with."""

    threshold: float = 0.5
    fields: dict[str, str] = Field(default_factory=lambda: {"From": "To"})
    # No renderer knows a list — this is what a save must not silently drop.
    extras: list[str] = Field(default_factory=lambda: ["kept"])


class TestTaskOptions:
    """What the settings panel renders, and what it has to carry through a save untouched."""

    def _split(self, **overrides: Any) -> TaskOptions:
        return TaskOptions(_DemoConfig(**overrides))

    def test_the_tick_owns_enable_so_the_form_never_renders_it(self):
        assert "enable" not in [name for name, _value, _field in self._split().rows]
        assert "enable" not in self._split().passthrough

    def test_scalars_are_rendered_with_their_descriptor(self):
        rows = {name: field for name, _value, field in self._split().rows}

        assert rows["threshold"] is not None
        assert rows["threshold"].kind == "float"

    def test_a_field_mapping_is_rendered_by_the_bespoke_editor(self):
        rows = {name: field for name, _value, field in self._split().rows}

        assert "fields" in rows and rows["fields"] is None

    def test_an_option_no_renderer_knows_is_kept_verbatim(self):
        # The ADR-010 hazard one layer up: writing back only what the form could draw would
        # delete a task option a NEWER Omnia added, on the next sync.
        split = self._split(extras=["one", "two"])

        assert split.passthrough == {"extras": ["one", "two"]}
        assert "extras" not in [name for name, _value, _field in split.rows]

    def test_an_option_key_from_a_newer_omnia_is_kept_verbatim(self):
        # The other half of the ADR-010 round trip: the task's model keeps the unknown key
        # (it is a PersistedModel), and the form has to hand it back to the save unchanged.
        split = TaskOptions(_DemoConfig.parse_obj({"from_a_newer_omnia": "kept"}))

        assert split.passthrough["from_a_newer_omnia"] == "kept"

    def test_an_unknown_key_holding_a_table_is_not_mistaken_for_a_field_map(self):
        # Only a DECLARED mapping option gets the bespoke editor: rendering an unknown table
        # in it would rewrite the newer Omnia's values as {str: str} — and the row label,
        # which reads the model's field info, would not even find the field.
        split = TaskOptions(
            _DemoConfig.parse_obj({"from_a_newer_omnia": {"nested": 1}})
        )

        assert split.passthrough["from_a_newer_omnia"] == {"nested": 1}
        assert "from_a_newer_omnia" not in [name for name, _v, _f in split.rows]

    def test_the_rows_keep_the_models_order(self):
        assert [name for name, _value, _field in self._split().rows] == [
            "order",
            "threshold",
            "fields",
        ]


class TestTaskSectionMerge:
    """``update_section`` replaces the whole map, so the merge decides what still exists."""

    def test_an_edited_task_gets_its_switch_and_options(self):
        merge = TaskSectionMerge({})

        merge.apply("demo", enable=True, options={"order": 5})

        assert merge.result() == {"demo": {"enable": True, "order": 5}}

    def test_a_stored_option_the_form_never_reported_survives(self):
        merge = TaskSectionMerge({"demo": {"order": 5, "a_future_option": ["kept"]}})

        merge.apply("demo", enable=False, options={"order": 7})

        assert merge.result()["demo"] == {
            "enable": False,
            "order": 7,
            "a_future_option": ["kept"],
        }

    def test_a_task_section_this_build_does_not_register_survives(self):
        merge = TaskSectionMerge({"a_future_task": {"enable": True, "mode": "beta"}})

        merge.apply("demo", enable=True, options={})

        assert merge.result()["a_future_task"] == {"enable": True, "mode": "beta"}

    def test_a_stored_entry_that_is_not_a_table_survives_untouched(self):
        # A hand-edited (or newer-Omnia) entry this version cannot even read as a section: it
        # is still the user's data, and this build must not be the one that deletes it.
        merge = TaskSectionMerge({"weird": "a string"})

        merge.apply("demo", enable=True, options={})

        assert merge.result()["weird"] == "a string"

    def test_editing_a_task_whose_entry_is_not_a_table_replaces_it(self):
        merge = TaskSectionMerge({"demo": "a string"})

        merge.apply("demo", enable=True, options={"order": 5})

        assert merge.result()["demo"] == {"enable": True, "order": 5}

    def test_the_stored_map_is_not_mutated(self):
        stored: dict[str, Any] = {"demo": {"order": 5}}
        merge = TaskSectionMerge(stored)

        merge.apply("demo", enable=False, options={"order": 9})

        assert stored == {"demo": {"order": 5}}

    def test_the_tick_wins_over_a_stored_enable(self):
        merge = TaskSectionMerge({"demo": {"enable": True}})

        merge.apply("demo", enable=False, options={})

        assert merge.result()["demo"]["enable"] is False
