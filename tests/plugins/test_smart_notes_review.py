"""Tests for the review-time pre-generation evaluator (best-effort, current-card-only).

Exercises the gating + current-card-only behaviour by faking the ``anki_compat`` seams the
evaluator calls (background run, note read/write, reviewer redraw). The ``run_in_background``
fake runs inline, so a tick fully generates + writes within the call.
"""

from __future__ import annotations

from conftest import FakeLLMProvider

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration.review import ReviewTimeEvaluator


class _FakeNote:
    def __init__(self, nid, note_type, fields):
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)

    def keys(self):
        return list(self._fields.keys())

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]

    def __setitem__(self, key, value):
        self._fields[key] = value

    def note_type(self):
        return {"name": self._note_type}


class _FakeCard:
    def __init__(self, note, did=1):
        self._note = note
        self.nid = note.id
        self.did = did

    def note(self):
        return self._note


class _StubHub:
    def __init__(self, llm):
        self._llm = llm

    def llm(self, *, model="", provider=""):
        return self._llm

    def tts(self):
        raise AssertionError("no TTS here")


class _FakeCompat:
    def __init__(self, notes, current=None):
        self._notes = notes
        self._current = current
        self.updated = []
        self.redraws = 0

    def get_note(self, nid, col=None):
        return self._notes[nid]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def add_media_file(self, filename, data, col=None):
        return filename

    def current_card(self):
        return self._current

    def redraw_reviewer_current_card(self):
        self.redraws += 1

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:
            if on_failure:
                on_failure(exc)


def _patch(monkeypatch, fake):
    import omnia.plugins.smart_notes.integration.review as rev

    for name in (
        "get_note",
        "update_note",
        "add_media_file",
        "current_card",
        "redraw_reviewer_current_card",
        "run_in_background",
    ):
        monkeypatch.setattr(rev.anki_compat, name, getattr(fake, name))


def _service():
    return GenerationService(_StubHub(FakeLLMProvider(text="generated")))


def _note_type_config():
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Def", enabled=True, type="text", prompt="define {{Word}}"
            )
        ],
    )


class TestReviewTimeEvaluator:
    def test_disabled_by_default_does_nothing(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        fake = _FakeCompat({1: note}, current=_FakeCard(note))
        _patch(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()]
        )  # generate_at_review defaults off
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(_FakeCard(note))
        assert fake.updated == []

    def test_generates_and_redraws_current_card(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        card = _FakeCard(note)
        fake = _FakeCompat({1: note}, current=card)
        _patch(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()], generate_at_review=True
        )
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(card)
        assert fake.updated == [1]
        assert note["Def"] == "generated"
        assert fake.redraws == 1

    def test_skips_when_target_already_filled(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": "filled"})
        fake = _FakeCompat({1: note}, current=_FakeCard(note))
        _patch(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()], generate_at_review=True
        )
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(_FakeCard(note))
        assert fake.updated == []

    def test_no_redraw_when_card_moved_on(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        other = _FakeNote(2, "Basic", {"Word": "dog", "Def": ""})
        # The reviewer has moved on to note 2 by the time generation completes.
        fake = _FakeCompat({1: note}, current=_FakeCard(other))
        _patch(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()], generate_at_review=True
        )
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(_FakeCard(note))
        assert fake.updated == [1]
        assert fake.redraws == 0

    def test_failure_is_swallowed(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        fake = _FakeCompat({1: note}, current=_FakeCard(note))

        def explode(op, **_k):
            raise RuntimeError("scheduling boom")

        fake.run_in_background = explode
        _patch(monkeypatch, fake)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()], generate_at_review=True
        )
        # on_card_shown wraps everything: a thrown scheduling error must not propagate.
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(_FakeCard(note))

    def test_card_out_of_deck_scope_is_not_generated(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        # The card lives in deck 9, but the config is scoped to deck 1 only.
        card = _FakeCard(note, did=9)
        fake = _FakeCompat({1: note}, current=card)
        _patch(monkeypatch, fake)
        config = _note_type_config().copy(update={"decks": [1]})
        settings = SmartNotesSettings(note_types=[config], generate_at_review=True)
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(card)
        assert fake.updated == []

    def test_card_in_deck_scope_is_generated(self, monkeypatch):
        note = _FakeNote(1, "Basic", {"Word": "cat", "Def": ""})
        card = _FakeCard(note, did=1)
        fake = _FakeCompat({1: note}, current=card)
        _patch(monkeypatch, fake)
        config = _note_type_config().copy(update={"decks": [1]})
        settings = SmartNotesSettings(note_types=[config], generate_at_review=True)
        ReviewTimeEvaluator(_service(), lambda: settings).on_card_shown(card)
        assert fake.updated == [1]
