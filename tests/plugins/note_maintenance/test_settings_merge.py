"""Tests for the save-time merge policy: what a settings save keeps, and what it may replace.

Pure — the classes under test carry no Qt, which is the point of their living in the plugin
rather than in the dialog that composes them.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from omnia.plugins.note_maintenance.base import OptionKind, TaskConfigBase
from omnia.plugins.note_maintenance.settings_merge import (
    NoteTypeSectionMerge,
    TaskOptions,
    TaskSectionMerge,
)


class _DemoConfig(TaskConfigBase):
    """A task config with one option of each kind the panel has to deal with."""

    threshold: float = 0.5
    source_field: str = Field("From", renders_as=OptionKind.NOTE_FIELD)
    fields: dict[str, str] = Field(
        default_factory=lambda: {"From": "To"}, renders_as=OptionKind.FIELD_MAP
    )
    # No renderer knows a list — this is what a save must not silently drop.
    extras: list[str] = Field(default_factory=lambda: ["kept"])


class TestTaskOptions:
    """What the settings panel renders, and what it has to carry through a save untouched."""

    def _split(self, **overrides: Any) -> TaskOptions:
        return TaskOptions(_DemoConfig(**overrides))

    def _names(self, split: TaskOptions) -> list[str]:
        return [row.name for row in split.rows]

    def _rows(self, split: TaskOptions) -> dict[str, Any]:
        return {row.name: row for row in split.rows}

    def test_the_tick_owns_enable_so_the_form_never_renders_it(self):
        assert "enable" not in self._names(self._split())
        assert "enable" not in self._split().passthrough

    def test_scalars_are_rendered_with_their_descriptor(self):
        row = self._rows(self._split())["threshold"]

        assert row.kind is OptionKind.SCALAR
        assert row.field is not None and row.field.kind == "float"

    def test_an_option_holding_a_field_name_is_rendered_as_a_field(self):
        # A field name typed by hand fails silently at run time, so the task declares that the
        # option names a FIELD and the panel offers the note type's real ones.
        row = self._rows(self._split())["source_field"]

        assert row.kind is OptionKind.NOTE_FIELD
        # It still carries the scalar descriptor — the label and help come from there.
        assert row.field is not None and row.field.label == "Source field"

    def test_a_field_mapping_is_rendered_by_the_bespoke_editor(self):
        row = self._rows(self._split())["fields"]

        assert row.kind is OptionKind.FIELD_MAP
        assert row.field is None

    def test_an_option_no_renderer_knows_is_kept_verbatim(self):
        # The ADR-010 hazard one layer up: writing back only what the form could draw would
        # delete a task option a NEWER Omnia added, on the next sync.
        split = self._split(extras=["one", "two"])

        assert split.passthrough == {"extras": ["one", "two"]}
        assert "extras" not in self._names(split)

    def test_an_option_key_from_a_newer_omnia_is_kept_verbatim(self):
        # The other half of the ADR-010 round trip: the task's model keeps the unknown key
        # (it is a PersistedModel), and the form has to hand it back to the save unchanged.
        split = TaskOptions(_DemoConfig.parse_obj({"from_a_newer_omnia": "kept"}))

        assert split.passthrough["from_a_newer_omnia"] == "kept"

    def test_an_unknown_key_holding_a_table_is_not_mistaken_for_a_field_map(self):
        # Only a DECLARED field map gets the bespoke editor: rendering an unknown table in it
        # would rewrite the newer Omnia's values as {field: field} — and the row label, which
        # reads the model's field info, would not even find the field.
        split = TaskOptions(
            _DemoConfig.parse_obj({"from_a_newer_omnia": {"nested": 1}})
        )

        assert split.passthrough["from_a_newer_omnia"] == {"nested": 1}
        assert "from_a_newer_omnia" not in self._names(split)

    def test_a_declared_table_that_is_not_a_field_map_is_carried_not_drawn(self):
        class _Config(TaskConfigBase):
            counts: dict[str, int] = Field(default_factory=lambda: {"a": 1})

        split = TaskOptions(_Config())

        assert split.passthrough == {"counts": {"a": 1}}

    def test_the_rows_keep_the_models_order(self):
        assert self._names(self._split()) == [
            "order",
            "threshold",
            "source_field",
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


class TestNoteTypeSectionMerge:
    """The same policy one level up, where a whole note type's settings are at stake."""

    def test_an_edited_note_type_gets_its_tick_and_its_tasks(self):
        merge = NoteTypeSectionMerge({})

        merge.apply("Vocab", enable=True, tasks={"strip_ipa": {"enable": True}})

        assert merge.result() == {
            "Vocab": {"enable": True, "tasks": {"strip_ipa": {"enable": True}}}
        }

    def test_a_note_type_the_user_never_opened_keeps_its_stored_tasks(self):
        # Ticking a note type must not be the same act as reconfiguring it.
        merge = NoteTypeSectionMerge(
            {"Vocab": {"enable": False, "tasks": {"strip_ipa": {"order": 5}}}}
        )

        merge.apply("Vocab", enable=True)

        assert merge.result()["Vocab"] == {
            "enable": True,
            "tasks": {"strip_ipa": {"order": 5}},
        }

    def test_a_note_type_this_build_cannot_see_survives(self):
        # Renamed, deleted, or living on another device — its settings are still the user's.
        merge = NoteTypeSectionMerge({"Gone": {"enable": True, "tasks": {"x": {}}}})

        merge.apply("Vocab", enable=True, tasks={})

        assert merge.result()["Gone"] == {"enable": True, "tasks": {"x": {}}}

    def test_an_unticked_note_type_keeps_its_settings(self):
        merge = NoteTypeSectionMerge(
            {"Vocab": {"enable": True, "tasks": {"strip_ipa": {"order": 5}}}}
        )

        merge.apply("Vocab", enable=False)

        assert merge.result()["Vocab"]["tasks"] == {"strip_ipa": {"order": 5}}
        assert merge.result()["Vocab"]["enable"] is False

    def test_a_key_from_a_newer_omnia_inside_an_entry_survives(self):
        merge = NoteTypeSectionMerge({"Vocab": {"enable": True, "scope": "deck:X"}})

        merge.apply("Vocab", enable=True, tasks={})

        assert merge.result()["Vocab"]["scope"] == "deck:X"

    def test_an_entry_that_is_not_a_table_survives_untouched(self):
        merge = NoteTypeSectionMerge({"Weird": "a string"})

        merge.apply("Vocab", enable=True, tasks={})

        assert merge.result()["Weird"] == "a string"

    def test_the_stored_map_is_not_mutated(self):
        stored: dict[str, Any] = {"Vocab": {"enable": True}}
        merge = NoteTypeSectionMerge(stored)

        merge.apply("Vocab", enable=False, tasks={"strip_ipa": {}})

        assert stored == {"Vocab": {"enable": True}}
