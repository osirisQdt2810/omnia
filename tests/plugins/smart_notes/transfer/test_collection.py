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


class TestTheMappingComesFromAnUntrustedPage:
    def test_an_explicitly_empty_mapping_is_honoured_not_re_derived(self):
        """Setting every row to "— not imported —" sends {}. Falling back to the suggestion
        there would import everything the user just declined."""
        col = FakeCollection()
        _seed(col)

        plan = plan_import(col, _bundle(col), mode=MODE_OVERWRITE, renames={})

        assert plan.renames == {}
        assert sorted(plan.unmapped_source_fields) == sorted(SOURCE_FIELDS)

    def test_no_mapping_at_all_still_suggests_one(self):
        col = FakeCollection()
        _seed(col)

        plan = plan_import(col, _bundle(col), mode=MODE_OVERWRITE)

        assert plan.renames == {name: name for name in SOURCE_FIELDS}

    def test_a_target_that_is_not_a_field_of_the_note_type_is_refused(self):
        """The page's <select> constrains this today — which is exactly why leaving it
        unchecked here would go unnoticed if it ever stopped."""
        col = FakeCollection()
        _seed(col)

        with pytest.raises(TransferError, match="not fields of"):
            plan_import(
                col,
                _bundle(col),
                mode=MODE_OVERWRITE,
                renames={"Word": "Nonexistent"},
            )


class TestWhatOverwriteDoesToTheTargetsOwnSetup:
    """Overwrite means "put the file's setup onto this note type", not "delete the parts the
    file has nothing to say about". The distinction is the difference between an import and
    silent data loss: the mapping table is the user's statement of what to replace, and a rule
    it never mentions is work they never offered up.
    """

    def _target_with_local_rules(self):
        """A collection whose ``Vocab`` has rules on Meaning AND Audio."""
        col = FakeCollection()
        col.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning", "Audio"])
        col.set_config(
            SMART_NOTES_KEY,
            {
                "note_types": [
                    _config(
                        fields=[
                            SmartNotesFieldConfig(
                                field="Meaning", enabled=True, prompt="local meaning"
                            ),
                            SmartNotesFieldConfig(
                                field="Audio", enabled=True, prompt="local audio prompt"
                            ),
                        ]
                    ).dict()
                ]
            },
        )
        return col

    def _incoming(self):
        """A bundle that configures ``Meaning`` only."""
        source = FakeCollection()
        _seed(
            source,
            config=_config(
                fields=[
                    SmartNotesFieldConfig(
                        field="Meaning", enabled=True, prompt="imported meaning"
                    )
                ]
            ),
        )
        return _bundle(source)

    def _apply(self, col, bundle, renames):
        plan = plan_import(
            col, bundle, mode=MODE_OVERWRITE, target_name="Vocab", renames=renames
        )
        apply_bundle(col, bundle, plan)
        entry = col.get_config(SMART_NOTES_KEY)["note_types"][0]
        return plan, {f["field"]: f["prompt"] for f in entry["fields"]}

    def test_a_rule_on_a_field_the_mapping_never_touches_survives(self):
        col = self._target_with_local_rules()

        _plan, rules = self._apply(col, self._incoming(), {"Meaning": "Meaning"})

        assert rules["Audio"] == "local audio prompt"

    def test_the_mapped_field_does_take_the_file_s_rule(self):
        col = self._target_with_local_rules()

        _plan, rules = self._apply(col, self._incoming(), {"Meaning": "Meaning"})

        assert rules["Meaning"] == "imported meaning"

    def test_the_warning_says_what_actually_happens(self):
        """This warning is read while the import can still be cancelled; it having said the
        opposite of the write is worse than it not being there at all.
        """
        col = self._target_with_local_rules()

        plan, rules = self._apply(col, self._incoming(), {"Meaning": "Meaning"})

        kept = " ".join(w for w in plan.warnings if "Audio" in w)
        assert "kept as they are" in kept
        assert rules["Audio"] == "local audio prompt"

    def test_a_field_the_mapping_redirects_away_from_keeps_its_local_rule(self):
        """``Meaning`` mapped onto ``Sentence`` leaves the local Meaning rule untouched: the
        import wrote Sentence, and said nothing about Meaning.
        """
        col = self._target_with_local_rules()

        _plan, rules = self._apply(col, self._incoming(), {"Meaning": "Sentence"})

        assert rules["Sentence"] == "imported meaning"
        assert rules["Meaning"] == "local meaning"
        assert rules["Audio"] == "local audio prompt"

    def test_a_target_with_no_configuration_of_its_own_is_unaffected(self):
        col = FakeCollection()
        col.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        _plan, rules = self._apply(col, self._incoming(), {"Meaning": "Meaning"})

        assert rules == {"Meaning": "imported meaning"}


class TestWhatOverwriteDoesToTheRestOfTheConfig:
    """The rules are not the whole configuration. ``base_field``, ``decks`` and
    ``node_positions`` are not rows in the mapping table the user is reading, and all three
    change what generates — so an overwrite that takes them wholesale from the file destroys
    settings nobody was asked about, and the first version of the merge did exactly that.
    """

    def _local(self):
        """``Vocab``: base ``Word``, a prompt-less TTS rule on ``Audio``, scoped to Japanese."""
        col = FakeCollection()
        col.models.add_note_type("Vocab", ["Word", "Reading", "Audio"])
        col.decks.add("Japanese", 42)
        col.set_config(
            SMART_NOTES_KEY,
            {
                "note_types": [
                    SmartNotesNoteTypeConfig(
                        note_type="Vocab",
                        base_field="Word",
                        decks=[42],
                        node_positions={"Audio": [10.0, 20.0]},
                        fields=[
                            SmartNotesFieldConfig(
                                field="Audio", enabled=True, prompt=""
                            )
                        ],
                    ).dict()
                ]
            },
        )
        return col

    def _incoming(self):
        """A colleague's ``Vocab``: base ``Term`` (no counterpart here), no deck restriction."""
        source = FakeCollection()
        source.models.add_note_type("Vocab", ["Term", "Reading"])
        source.set_config(
            SMART_NOTES_KEY,
            {
                "note_types": [
                    SmartNotesNoteTypeConfig(
                        note_type="Vocab",
                        base_field="Term",
                        fields=[
                            SmartNotesFieldConfig(
                                field="Reading", enabled=True, prompt="Read {{Term}}"
                            )
                        ],
                    ).dict()
                ]
            },
        )
        return build_bundle(source, "Vocab")

    def _apply(self):
        col = self._local()
        bundle = self._incoming()
        # ``Term`` has no counterpart here, so that row stays on "— not imported —".
        plan = plan_import(
            col,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Vocab",
            renames={"Reading": "Reading"},
        )
        apply_bundle(col, bundle, plan)
        entry = col.get_config(SMART_NOTES_KEY)["note_types"][0]
        return plan, SmartNotesNoteTypeConfig(**entry)

    def test_an_unmapped_incoming_base_field_leaves_the_local_one_alone(self):
        """A cleared base field is the nastiest outcome of the three: every prompt-less rule
        then compiles with ``source_field=""`` and generates from nothing — kept, but inert,
        with nothing on screen saying so.
        """
        _plan, out = self._apply()

        assert out.base_field == "Word"

    def test_the_deck_restriction_is_not_silently_widened(self):
        """``decks=[]`` does not mean "no restriction carried" — it means ALL decks. Taking
        the file's empty list switches generation, and the spend that goes with it, on in
        decks the user deliberately excluded.
        """
        _plan, out = self._apply()

        assert out.decks == [42]

    def test_a_kept_rule_keeps_its_place_on_the_graph(self):
        _plan, out = self._apply()

        assert out.node_positions.get("Audio") == [10.0, 20.0]

    def test_the_mapped_rule_still_comes_from_the_file(self):
        _plan, out = self._apply()

        rules = {rule.field: rule.prompt for rule in out.fields}
        assert rules["Reading"] == "Read {{Term}}"
        assert rules["Audio"] == ""

    def test_the_user_is_told_the_base_field_is_staying(self):
        plan, _out = self._apply()

        assert any(
            "'Term'" in w and "'Word' stays the base field" in w for w in plan.warnings
        )

    def test_the_user_is_told_the_deck_restriction_is_staying(self):
        plan, _out = self._apply()

        assert any("keeps its own (Japanese)" in w for w in plan.warnings)

    def test_a_mapped_base_field_does_replace_the_local_one(self):
        """The other half: when the file's base field HAS a counterpart, it wins — and the
        user is told, because every prompt-less rule now generates from a different field.
        """
        col = self._local()
        bundle = self._incoming()

        plan = plan_import(
            col,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Vocab",
            renames={"Term": "Reading", "Reading": "Audio"},
        )
        apply_bundle(col, bundle, plan)

        entry = col.get_config(SMART_NOTES_KEY)["note_types"][0]
        assert SmartNotesNoteTypeConfig(**entry).base_field == "Reading"
        assert any(
            "base field changes from 'Word' to 'Reading'" in w for w in plan.warnings
        )

    def test_the_kept_fields_warning_names_rules_not_bare_fields(self):
        """``unused_target_fields`` includes fields with no rule at all — the base field among
        them. Telling the user their rules are kept is noise there, and wrong for the base.
        """
        plan, _out = self._apply()

        assert plan.kept_local_fields == ["Audio"]
        assert "Word" in plan.unused_target_fields
        kept = [w for w in plan.warnings if "are kept as they are" in w]
        assert kept and "Word" not in kept[0]

    def test_a_key_only_a_newer_omnia_knows_survives_the_import(self):
        """ADR-010: unknown keys round-trip. An import from an older Omnia must not strip the
        local entry's newer ones — same rule as the rest of the merge, one level up.
        """
        col = self._local()
        blob = col.get_config(SMART_NOTES_KEY)
        blob["note_types"][0]["future_flag"] = {"kept": True}
        col.set_config(SMART_NOTES_KEY, blob)
        bundle = self._incoming()

        plan = plan_import(
            col,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Vocab",
            renames={"Reading": "Reading"},
        )
        apply_bundle(col, bundle, plan)

        entry = col.get_config(SMART_NOTES_KEY)["note_types"][0]
        assert entry.get("future_flag") == {"kept": True}
