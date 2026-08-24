"""Reading a bundle out of a collection, planning an import, and applying one.

The fake collection is the point of the module's injected ``col``/``tool_loader``: everything
here — the version gate, every refusal ``plan_import`` can make, the ``id``/``usn``/``mod``
zeroing Anki's backend demands, and the read-modify-write that must not clobber the OTHER note
types' setups — is behaviour a user only ever sees when it is wrong.
"""

from __future__ import annotations

import json

import pytest
from transfer_fakes import FakeCollection, FakeToolStore

from omnia.plugins.smart_notes.config import (
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.transfer.bundle import (
    BUNDLE_VERSION,
    BundleError,
    NoteTypeBundle,
    parse_bundle,
)
from omnia.plugins.smart_notes.transfer.collection import (
    MODE_CLONE,
    MODE_CREATE,
    MODE_OVERWRITE,
    SMART_NOTES_KEY,
    TransferError,
    apply_bundle,
    build_bundle,
    plan_import,
)

SOURCE_FIELDS = ["Word", "Sentence", "Meaning"]


def _config(**overrides) -> SmartNotesNoteTypeConfig:
    base = dict(
        note_type="Vocab",
        base_field="Word",
        node_positions={"Sentence": [1.0, 2.0]},
        fields=[
            SmartNotesFieldConfig(
                field="Sentence", enabled=True, prompt="A sentence for {{Word}}"
            )
        ],
    )
    base.update(overrides)
    return SmartNotesNoteTypeConfig(**base)


def _seed(col, name="Vocab", fields=None, config=None):
    col.models.add_note_type(name, fields or SOURCE_FIELDS)
    col.set_config(SMART_NOTES_KEY, {"note_types": [(config or _config()).dict()]})


class TestBuildingABundle:
    def test_it_carries_the_schema_the_config_and_the_tools(self):
        col = FakeCollection()
        _seed(col)
        bundle = build_bundle(
            col, "Vocab", tool_store=FakeToolStore({"mine": "# tool"}), profile="P"
        )

        assert bundle.note_type_name == "Vocab"
        assert bundle.field_names() == SOURCE_FIELDS
        assert bundle.anki_note_type["css"] == ".card { font-size: 20px; }"
        assert bundle.smart_notes.base_field == "Word"
        assert bundle.source.profile == "P"

    def test_deck_ids_are_exported_as_names(self):
        """Deck ids are per-collection integers; the same id elsewhere is a different deck."""
        col = FakeCollection()
        col.decks.add("Spanish::Verbs", 42)
        _seed(col, config=_config(decks=[42]))

        bundle = build_bundle(col, "Vocab")

        assert bundle.deck_names == ["Spanish::Verbs"]

    def test_a_deck_that_no_longer_exists_is_simply_left_out(self):
        col = FakeCollection()
        _seed(col, config=_config(decks=[999]))

        assert build_bundle(col, "Vocab").deck_names == []

    def test_only_the_user_tools_the_chains_reference_are_carried(self):
        col = FakeCollection()
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Sentence", tools=[FieldToolConfig(tool="user:wanted")]
                )
            ]
        )
        _seed(col, config=config)
        store = FakeToolStore({"wanted": "# yes", "unrelated": "# no"})

        bundle = build_bundle(col, "Vocab", tool_store=store)

        assert list(bundle.user_tools) == ["user:wanted"]
        assert bundle.missing_user_tools() == []

    def test_a_referenced_tool_with_no_file_is_reported_as_missing(self):
        col = FakeCollection()
        config = _config(
            fields=[
                SmartNotesFieldConfig(
                    field="Sentence", tools=[FieldToolConfig(tool="user:gone")]
                )
            ]
        )
        _seed(col, config=config)

        bundle = build_bundle(col, "Vocab", tool_store=FakeToolStore())

        assert bundle.missing_user_tools() == ["user:gone"]

    def test_an_unknown_note_type_is_refused(self):
        col = FakeCollection()
        with pytest.raises(TransferError, match="no note type"):
            build_bundle(col, "Nope")

    def test_a_note_type_with_no_configuration_is_refused(self):
        """Exporting nothing at all is a mistake worth saying out loud."""
        col = FakeCollection()
        col.models.add_note_type("Bare", ["A"])

        with pytest.raises(TransferError, match="no Smart Notes configuration"):
            build_bundle(col, "Bare")


class TestTheVersionGate:
    def test_a_bundle_from_a_newer_omnia_is_refused(self):
        """Half-understanding a format is worse than a clear refusal: a newer bundle may
        encode a chain shape this one would silently drop."""
        raw = json.dumps({"bundle_version": BUNDLE_VERSION + 1, "note_type_name": "X"})

        with pytest.raises(BundleError, match="newer Omnia"):
            parse_bundle(raw)

    def test_the_current_version_round_trips(self):
        col = FakeCollection()
        _seed(col)
        text = build_bundle(col, "Vocab").to_json()

        assert parse_bundle(text).note_type_name == "Vocab"

    def test_a_file_that_is_not_json(self):
        with pytest.raises(BundleError, match="not valid JSON"):
            parse_bundle("{oops")

    def test_json_that_is_not_a_bundle(self):
        with pytest.raises(BundleError, match="not an Omnia note-type bundle"):
            parse_bundle('{"hello": 1}')

    def test_a_json_array_is_not_a_bundle(self):
        with pytest.raises(BundleError, match="does not contain"):
            parse_bundle("[1, 2]")


def _bundle(col, name="Vocab") -> NoteTypeBundle:
    return build_bundle(col, name)


class TestPlanningAnImport:
    def test_a_free_name_plans_a_create(self):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()

        plan = plan_import(fresh, bundle)

        assert plan.mode == MODE_CREATE
        assert plan.creates_note_type is True
        assert plan.unmapped_source_fields == []

    def test_a_taken_name_plans_an_overwrite(self):
        col = FakeCollection()
        _seed(col)

        plan = plan_import(col, _bundle(col))

        assert plan.mode == MODE_OVERWRITE
        assert plan.creates_note_type is False
        assert plan.replaces_config is True

    def test_create_onto_a_taken_name_is_refused(self):
        col = FakeCollection()
        _seed(col)

        with pytest.raises(TransferError, match="already has a note type"):
            plan_import(col, _bundle(col), mode=MODE_CREATE)

    def test_cloning_onto_a_taken_name_is_refused(self):
        col = FakeCollection()
        _seed(col)

        with pytest.raises(TransferError, match="already taken"):
            plan_import(col, _bundle(col), mode=MODE_CLONE, target_name="Vocab")

    def test_overwriting_a_note_type_that_is_not_here_is_refused(self):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()

        with pytest.raises(TransferError, match="no note type called"):
            plan_import(fresh, bundle, mode=MODE_OVERWRITE, target_name="Vocab")

    def test_a_config_only_bundle_cannot_create_a_note_type(self):
        """Nothing to build the note type FROM — say so rather than half-importing."""
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col).copy(update={"anki_note_type": None})
        fresh = FakeCollection()

        with pytest.raises(TransferError, match="configuration only"):
            plan_import(fresh, bundle, mode=MODE_CREATE)

    def test_unmapped_fields_are_named_in_the_warnings(self):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        col = FakeCollection()
        col.models.add_note_type("Other", ["Word"])

        plan = plan_import(
            col,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Other",
            renames={"Word": "Word"},
        )

        assert plan.unmapped_source_fields == ["Sentence", "Meaning"]
        assert any("will be dropped" in w for w in plan.warnings)

    def test_two_source_fields_onto_one_target_is_refused(self):
        """The page refuses it too, but the page is the untrusted side of a pycmd boundary."""
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        col = FakeCollection()
        col.models.add_note_type("Other", ["Word"])

        with pytest.raises(TransferError, match="both map onto the same field"):
            plan_import(
                col,
                bundle,
                mode=MODE_OVERWRITE,
                target_name="Other",
                renames={"Word": "Word", "Sentence": "Word"},
            )

    def test_a_missing_deck_is_reported(self):
        col = FakeCollection()
        col.decks.add("Kept", 7)
        _seed(col, config=_config(decks=[7]))
        bundle = _bundle(col)
        fresh = FakeCollection()

        plan = plan_import(fresh, bundle)

        assert plan.missing_decks == ["Kept"]


class TestApplyingABundle:
    def test_create_adds_the_note_type_and_its_config(self):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()

        result = apply_bundle(fresh, bundle, plan_import(fresh, bundle))

        assert result.created_note_type is True
        assert fresh.models.by_name("Vocab") is not None
        entry = fresh.get_config(SMART_NOTES_KEY)["note_types"][0]
        assert entry["note_type"] == "Vocab"
        assert entry["base_field"] == "Word"

    def test_the_added_note_type_is_stamped_for_THIS_collection(self):
        """``id``/``usn``/``mod`` describe the OTHER collection. Anki's backend also REQUIRES
        both keys to be present — dropping them fails the add with a JsonError."""
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()

        apply_bundle(fresh, bundle, plan_import(fresh, bundle))

        added = fresh.models.added[0]
        assert added["usn"] == 0
        assert added["mod"] == 0
        # id 0 is what tells Anki to mint a fresh one; keeping the source collection's id
        # would collide with an unrelated local note type, or graft this onto it.
        assert added["id"] == 0

    def test_clone_uses_the_new_name_everywhere(self):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()

        plan = plan_import(fresh, bundle, mode=MODE_CLONE, target_name="Vocab Copy")
        apply_bundle(fresh, bundle, plan)

        assert fresh.models.by_name("Vocab Copy") is not None
        entry = fresh.get_config(SMART_NOTES_KEY)["note_types"][0]
        assert entry["note_type"] == "Vocab Copy"

    def test_deck_names_resolve_back_to_this_collections_ids(self):
        col = FakeCollection()
        col.decks.add("Shared", 7)
        _seed(col, config=_config(decks=[7]))
        bundle = _bundle(col)
        fresh = FakeCollection()
        fresh.decks.add("Shared", 99)  # a DIFFERENT id for the same deck name

        apply_bundle(fresh, bundle, plan_import(fresh, bundle))

        assert fresh.get_config(SMART_NOTES_KEY)["note_types"][0]["decks"] == [99]

    def test_the_other_note_types_setups_survive(self):
        """The config key holds every note type together; a careless write loses the rest."""
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)
        fresh = FakeCollection()
        fresh.set_config(
            SMART_NOTES_KEY,
            {
                "note_types": [_config(note_type="Untouched").dict()],
                "generate_at_review": True,
            },
        )

        apply_bundle(fresh, bundle, plan_import(fresh, bundle))

        blob = fresh.get_config(SMART_NOTES_KEY)
        assert [e["note_type"] for e in blob["note_types"]] == ["Untouched", "Vocab"]
        assert blob["generate_at_review"] is True

    def test_re_importing_replaces_that_note_types_entry_rather_than_duplicating_it(
        self,
    ):
        col = FakeCollection()
        _seed(col)
        bundle = _bundle(col)

        apply_bundle(col, bundle, plan_import(col, bundle))
        apply_bundle(col, bundle, plan_import(col, bundle))

        entries = col.get_config(SMART_NOTES_KEY)["note_types"]
        assert [e["note_type"] for e in entries] == ["Vocab"]
