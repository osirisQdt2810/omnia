"""Tests for what a note-field dropdown offers (pure logic).

The rule that matters is the one about a value the note type no longer has: it is KEPT and
MARKED, never dropped. Dropping it would let a save rewrite the user's setting to whatever
landed at index 0 — the silent data loss this plugin has already shipped three of.
"""

from __future__ import annotations

from omnia.plugins.note_maintenance.field_choices import (
    STALE_SUFFIX,
    UNSET_LABEL,
    FieldChoices,
)

_FIELDS = ("Word", "Synonyms", "SynonymsNoIPA")


def _values(choices: FieldChoices, value: str) -> list[str]:
    return [choice.value for choice in choices.entries(value)]


class TestFieldChoices:
    def test_the_note_types_own_fields_are_offered_in_their_own_order(self):
        assert _values(FieldChoices(_FIELDS), "Word") == list(_FIELDS)

    def test_the_stored_value_is_selectable(self):
        entries = FieldChoices(_FIELDS).entries("Synonyms")

        assert [entry for entry in entries if entry.value == "Synonyms"]

    def test_a_value_the_note_type_no_longer_has_is_kept_and_marked(self):
        entries = FieldChoices(_FIELDS).entries("OldName")

        stale = entries[-1]
        assert (
            stale.value == "OldName"
        )  # what a save writes back is the value, not the label
        assert stale.is_stale
        assert stale.label == f"OldName{STALE_SUFFIX}"
        assert [entry for entry in entries if entry.is_stale] == [stale]

    def test_a_note_type_this_collection_does_not_have_keeps_every_value(self):
        # No fields to offer at all — the stored value is still the selected entry.
        entries = FieldChoices(()).entries("Synonyms")

        assert [(entry.value, entry.is_stale) for entry in entries] == [
            ("Synonyms", True)
        ]

    def test_a_blank_option_offers_its_own_meaning(self):
        entries = FieldChoices(_FIELDS, blank_label="(in place)").entries("Word")

        assert entries[0].value == ""
        assert entries[0].label == "(in place)"
        assert not entries[0].is_stale

    def test_an_option_with_no_blank_meaning_offers_none(self):
        assert "" not in _values(FieldChoices(_FIELDS), "Word")

    def test_a_stored_blank_is_still_offered_when_the_option_has_no_blank(self):
        # Otherwise the empty value could not be re-selected, and the dropdown would silently
        # promote the first field into a setting the user never made.
        entries = FieldChoices(_FIELDS).entries("")

        assert entries[0].value == ""
        assert entries[0].label == UNSET_LABEL

    def test_a_blank_is_offered_once_even_when_it_is_the_stored_value(self):
        entries = FieldChoices(_FIELDS, blank_label="(in place)").entries("")

        assert (
            _values(FieldChoices(_FIELDS, blank_label="(in place)"), "").count("") == 1
        )
        assert entries[0].label == "(in place)"
