"""Tests for the smart_notes batch generator (the cancellable, counted batch glue).

The batch runner is Anki glue, but its orchestration — dedupe, per-note rule selection,
chunked generation, cancel handling, and the success/fail/skip summary — is exercised here by
faking the ``anki_compat`` seams it calls (collection reads, progress dialog, media writes).
The ``run_in_background`` seam is the synchronous QueryOp stub from ``conftest``, so the whole
flow runs inline.
"""

from __future__ import annotations

from conftest import FakeLLMProvider

from omnia.core.config.models import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.batch import BatchGenerator
from omnia.plugins.smart_notes.logic import GenerationService


def _note_type_config(note_type="Basic", *, enabled=True):
    """A Basic note type whose 'Def' field is generated from the 'Word' base field."""
    return SmartNotesNoteTypeConfig(
        note_type=note_type,
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Def", enabled=enabled, type="text", prompt="define {{Word}}"
            )
        ],
    )


class _FakeNote:
    """A dict-like note exposing ``keys()`` + ``note_type()`` like Anki's Note."""

    def __init__(self, nid: int, note_type: str, fields: dict[str, str]) -> None:
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)

    def keys(self):
        return list(self._fields.keys())

    def __contains__(self, key: str) -> bool:
        return key in self._fields

    def __getitem__(self, key: str) -> str:
        return self._fields[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._fields[key] = value

    def note_type(self) -> dict[str, str]:
        return {"name": self._note_type}


class _StubHub:
    def __init__(self, llm) -> None:
        self._llm = llm

    def llm(self, *, model: str = "", provider: str = ""):
        return self._llm

    def tts(self):
        raise AssertionError("no TTS in these tests")


class _FakeCompat:
    """Records progress calls + note writes; drives cancel via a queued flag list."""

    def __init__(self, notes: dict[int, _FakeNote], cancel_after: int | None = None):
        self._notes = notes
        self.updated: list[int] = []
        self.progress: list[str] = []
        self.media: list[str] = []
        self._cancel_after = cancel_after
        self._cancel_polls = 0
        self.run_on_main_calls = 0

    # collection
    def get_note(self, nid, col=None):
        return self._notes[nid]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def add_media_file(self, filename, data, col=None):
        self.media.append(filename)
        return filename

    # progress
    def progress_start(self, label, maximum):
        self.progress.append(label)

    def progress_update(self, label, value, maximum):
        self.progress.append(label)

    def progress_finish(self):
        self.progress.append("finish")

    def progress_was_cancelled(self):
        self._cancel_polls += 1
        return (
            self._cancel_after is not None and self._cancel_polls > self._cancel_after
        )

    # threading
    def run_on_main(self, callback):
        self.run_on_main_calls += 1
        callback()

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:  # mirror QueryOp routing
            if on_failure:
                on_failure(exc)


def _patch_compat(monkeypatch, fake: _FakeCompat) -> None:
    import omnia.plugins.smart_notes.batch as batch

    for name in (
        "get_note",
        "update_note",
        "add_media_file",
        "progress_start",
        "progress_update",
        "progress_finish",
        "progress_was_cancelled",
        "run_on_main",
        "run_in_background",
    ):
        monkeypatch.setattr(batch.anki_compat, name, getattr(fake, name))


def _generator(settings: SmartNotesSettings) -> GenerationService:
    return GenerationService(_StubHub(FakeLLMProvider(text="generated")))


class TestBatchGenerator:
    def _settings(self, **kw) -> SmartNotesSettings:
        base = {
            "note_types": [_note_type_config()],
            "regenerate_when_batching": False,
        }
        base.update(kw)
        return SmartNotesSettings(**base)

    def test_generates_and_writes_each_note(self, monkeypatch):
        notes = {
            1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""}),
            2: _FakeNote(2, "Basic", {"Word": "dog", "Def": ""}),
        }
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1, 2], summaries.append)
        assert fake.updated == [1, 2]
        assert summaries[0].processed == 2
        assert notes[1]["Def"] == "generated"

    def test_dedupes_note_ids(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1, 1, 1], summaries.append)
        assert summaries[0].processed == 1
        assert fake.updated == [1]

    def test_already_filled_target_is_skipped(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "filled"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(regenerate_when_batching=False)
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].skipped == 1
        assert fake.updated == []

    def test_regenerate_when_batching_overwrites_filled_target(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": "old"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings(regenerate_when_batching=True)
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0].processed == 1
        assert notes[1]["Def"] == "generated"

    def test_notes_without_matching_rules_are_dropped(self, monkeypatch):
        notes = {1: _FakeNote(1, "Cloze", {"Text": "x"})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        assert summaries[0] == summaries[0]  # ran
        assert summaries[0].processed == 0
        assert fake.progress == []  # never opened progress for an empty plan

    def test_cancel_stops_before_finishing(self, monkeypatch):
        notes = {
            n: _FakeNote(n, "Basic", {"Word": "w", "Def": ""}) for n in range(1, 13)
        }
        # Cancel is polled once per chunk (size 5); allow the first poll, cancel the second.
        fake = _FakeCompat(notes, cancel_after=1)
        _patch_compat(monkeypatch, fake)
        settings = self._settings()
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run(
            list(range(1, 13)), summaries.append
        )
        assert summaries[0].cancelled is True
        # Only the first chunk (5) was generated before the cancel was honoured.
        assert summaries[0].processed == 5


class TestBatchGeneratorDisabledRules:
    def test_disabled_fields_are_skipped_in_batch(self, monkeypatch):
        notes = {1: _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})}
        fake = _FakeCompat(notes)
        _patch_compat(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config(enabled=False)],
            regenerate_when_batching=False,
        )
        summaries: list = []
        BatchGenerator(_generator(settings), settings).run([1], summaries.append)
        # No enabled, generatable field → empty plan → nothing happens.
        assert summaries[0].processed == 0
        assert fake.progress == []
