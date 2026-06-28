"""Tests for the collection-backed smart_notes settings store.

The store persists the per-note-type rules in the collection config (``get_config`` /
``set_config``) so they sync across devices. A fake collection with a plain dict exercises the
round-trip + the empty-collection default without needing a real Anki collection.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
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
