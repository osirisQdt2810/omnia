"""Tests for the collection-backed smart_notes settings store.

The store persists the per-note-type rules in the collection config (``get_config`` /
``set_config``) so they sync across devices. A fake collection with a plain dict exercises the
round-trip + the empty-collection default without needing a real Anki collection.

Because the blob syncs, devices running DIFFERENT Omnia versions read each other's writes —
hence the forward-compat suite: a blob carrying keys (or values) only a newer version knows
about must load here AND survive being written straight back.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.integration import SmartNotesStore


class _FakeCol:
    """A stand-in collection exposing ``get_config``/``set_config`` over a plain dict."""

    def __init__(self) -> None:
        self.conf: dict[str, object] = {}

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.conf[key] = value


def _settings() -> SmartNotesSettings:
    return SmartNotesSettings(
        note_types=[
            SmartNotesNoteTypeConfig(
                note_type="Basic",
                base_field="Word",
                fields=[
                    SmartNotesFieldConfig(
                        field="Def", enabled=True, type="text", prompt="define {{Word}}"
                    )
                ],
                decks=[1, 2],
            )
        ]
    )


def _blob_from_a_newer_client() -> dict:
    """Return a raw blob as a FUTURE Omnia would write it: unknown keys at every level.

    The unknown keys are the ones the tools work is slated to add (``tools`` per field,
    ``custom_tools`` globally) plus invented ones, so the fixture documents the real scenario
    this compat flip exists for. Every value here IS understood by this version — the unknown
    VALUES (a new generation type, a new dep kind) get their own tests below.
    """
    return {
        "note_types": [
            {
                "note_type": "Basic",
                "base_field": "Word",
                "fields": [
                    {
                        "field": "Def",
                        "enabled": True,
                        "type": "text",
                        "prompt": "define {{Word}}",
                        # field-level unknown keys
                        "tools": [{"tool": "cloze", "params": {}}],
                        "depends_on": [
                            # dep-level unknown key
                            {"field": "Word", "kind": "soft", "confidence": 0.75}
                        ],
                    }
                ],
                "decks": [1, 2],
                # note-type-level unknown key
                "note_type_icon": "star",
            }
        ],
        "allow_empty_fields": True,
        # top-level unknown keys
        "custom_tools": [{"name": "user:extract-ext", "steps": []}],
        "future_flag": True,
    }


class TestSmartNotesStore:
    def test_save_then_load_round_trips_note_types(self):
        fake = _FakeCol()
        store = SmartNotesStore(col_provider=lambda: fake)
        store.save(_settings())
        loaded = store.load()
        assert [nt.note_type for nt in loaded.note_types] == ["Basic"]
        nt = loaded.note_type_config("Basic")
        assert nt is not None
        assert nt.base_field == "Word"
        assert nt.decks == [1, 2]
        assert [f.field for f in nt.generatable_fields()] == ["Def"]

    def test_load_on_empty_collection_returns_default(self):
        store = SmartNotesStore(col_provider=lambda: _FakeCol())
        loaded = store.load()
        assert loaded.note_types == []

    def test_load_without_collection_returns_default(self):
        # A col_provider that fails (e.g. mw.col not ready) degrades to a default, not a crash.
        def boom():
            raise RuntimeError("col not ready")

        store = SmartNotesStore(col_provider=boom)
        assert store.load().note_types == []
        store.save(_settings())  # save is a silent no-op without a collection


class TestSyncedBlobForwardCompat:
    """An OLDER Omnia must survive — and never damage — a blob written by a NEWER one.

    The models are :class:`~omnia.core.config.base.PersistedModel`s (``extra = "allow"``), so
    unknown keys are kept AND written back verbatim. ``extra = "ignore"`` would also load, but
    ``store.save`` serializes ``settings.dict()`` — so the old client would silently erase the
    newer device's settings on its next write.
    """

    def test_load_keeps_known_values_when_blob_has_unknown_keys(self):
        fake = _FakeCol()
        fake.set_config(SmartNotesStore.KEY, _blob_from_a_newer_client())

        loaded = SmartNotesStore(col_provider=lambda: fake).load()

        assert loaded.allow_empty_fields is True
        nt = loaded.note_type_config("Basic")
        assert nt is not None
        assert nt.base_field == "Word"
        assert nt.decks == [1, 2]
        field = nt.fields[0]
        assert (field.field, field.type, field.prompt) == (
            "Def",
            "text",
            "define {{Word}}",
        )
        assert [(d.field, d.kind) for d in field.depends_on] == [("Word", "soft")]

    def test_save_after_load_preserves_every_unknown_key(self):
        # THE data-loss regression: an old client that loads and saves must hand the newer
        # device's keys back untouched, at every level of the tree.
        fake = _FakeCol()
        blob = _blob_from_a_newer_client()
        fake.set_config(SmartNotesStore.KEY, blob)
        store = SmartNotesStore(col_provider=lambda: fake)

        store.save(store.load())

        saved = fake.get_config(SmartNotesStore.KEY)
        assert saved["custom_tools"] == blob["custom_tools"]  # top level
        assert saved["future_flag"] is True
        note_type = saved["note_types"][0]
        assert note_type["note_type_icon"] == "star"  # note-type level
        field = note_type["fields"][0]
        assert (
            field["tools"] == blob["note_types"][0]["fields"][0]["tools"]
        )  # field level
        assert field["depends_on"][0]["confidence"] == 0.75  # dependency level
        # …and the known settings still work after the round trip.
        reloaded = store.load()
        nt = reloaded.note_type_config("Basic")
        assert nt is not None
        assert [f.field for f in nt.generatable_fields()] == ["Def"]

    def test_unsupported_field_type_loads_verbatim_and_never_generates(self):
        # A generation type only a NEWER Omnia implements: the blob must load, the row must be
        # kept EXACTLY as written (rewriting it here would make save() hand the damage back —
        # store.save serializes the whole tree, including note types this user never opened),
        # and this version must not generate the wrong kind of content into it.
        fake = _FakeCol()
        fake.set_config(
            SmartNotesStore.KEY,
            {
                "note_types": [
                    {
                        "note_type": "Basic",
                        "base_field": "Word",
                        "fields": [
                            {"field": "Clip", "enabled": True, "type": "video"},
                            {"field": "Def", "enabled": True, "type": "text"},
                        ],
                    }
                ]
            },
        )
        store = SmartNotesStore(col_provider=lambda: fake)

        settings = store.load()

        nt = settings.note_type_config("Basic")
        assert nt is not None
        assert [f.field for f in nt.generatable_fields()] == ["Def"]
        clip = nt.fields[0]
        assert (clip.type, clip.enabled) == ("video", True)
        # …and saving from this older device leaves the row's semantics intact.
        store.save(settings)
        saved_clip = fake.get_config(SmartNotesStore.KEY)["note_types"][0]["fields"][0]
        assert (saved_clip["type"], saved_clip["enabled"]) == ("video", True)

    def test_unsupported_field_type_compiles_to_an_inert_rule(self):
        # The graph / consistency views compile EVERY row, generatable or not, into a strict
        # SmartNotesFieldRule — so the unknown type must degrade there instead of raising.
        from omnia.plugins.smart_notes.engine.rules import compile_field_rule

        row = SmartNotesFieldConfig(field="Clip", enabled=True, type="video")

        rule = compile_field_rule(row, "Word")

        assert rule.kind == "text"
        assert row.supports_generation() is False

    def test_unknown_dep_kind_survives_and_never_blocks(self):
        # A dependency kind from a future release: kept verbatim (so the newer device gets it
        # back) and treated as the weaker "soft" semantics — it orders, it cannot block.
        from omnia.plugins.smart_notes.engine.graph import FieldGraph

        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Def",
                    enabled=True,
                    type="text",
                    prompt="define {{Word}}",
                    depends_on=[FieldDep(field="Word", kind="conditional")],
                )
            ],
        )

        assert config.dict()["fields"][0]["depends_on"][0]["kind"] == "conditional"
        edge = FieldGraph.from_config(config).edges[0]
        assert edge.kind == "conditional"
        assert config.fields[0].depends_on[0].kind != "hard"  # never blocks generation

    def test_unpersisted_rule_model_still_rejects_unknown_keys(self):
        # SmartNotesFieldRule is compiled in memory from GUI/engine input and never stored, so
        # strictness stays: an unknown key there is a typo, not a future version's data.
        with pytest.raises(ValidationError):
            SmartNotesFieldRule(target_field="Def", not_a_real_key=1)
