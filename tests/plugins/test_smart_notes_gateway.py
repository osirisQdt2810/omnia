"""Tests for the external-integration gateway (Feature B) and its config toggle.

Exercises the two-guard gating that decides whether a freshly-pushed note (e.g. an AnkiConnect
``addNote`` from the browser clipper) is auto-generated. The Anki seams are faked: ``aqt.mw``
carries a fake ``taskman.run_on_main`` (runs the deferred lambda inline) and a fake ``col``, and
:class:`BatchGenerator` is replaced with a recorder so no real generation runs. Pure guard logic,
headless.
"""

from __future__ import annotations

import types

from conftest import FakeLLMProvider

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration.gateway import IntegrationGateway
from omnia.plugins.smart_notes.integration.integrations import (
    AUTOGEN_TAG,
    INTEGRATIONS,
    integration_for_tags,
)

_SOURCE_TAG = "omnia-web-clipper"


class _FakeNote:
    def __init__(self, nid, note_type, fields, tags):
        self.id = nid
        self._note_type = note_type
        self._fields = dict(fields)
        self.tags = list(tags)

    def has_tag(self, tag):
        return tag in self.tags

    def remove_tag(self, tag):
        self.tags = [t for t in self.tags if t != tag]

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


class _FakeCol:
    def __init__(self, notes):
        self._notes = notes
        self.updated = []

    def get_note(self, nid):
        return self._notes[nid]

    def update_note(self, note):
        self.updated.append(note.id)


class _StubHub:
    def __init__(self, llm):
        self._llm = llm

    def llm(self, *, model="", provider=""):
        return self._llm

    def tts(self):
        raise AssertionError("no TTS here")


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


def _install(monkeypatch, notes, *, run_deferred=True):
    """Wire ``aqt.mw`` + a recording ``BatchGenerator``; return ``(col, batch_calls)``."""
    import aqt

    import omnia.plugins.smart_notes.integration.gateway as gw

    batch_calls: list[list[int]] = []

    class _FakeBatch:
        def __init__(self, service, settings):
            self._service = service
            self._settings = settings

        def run(self, note_ids, _on_done, *, show_progress=True):
            # Background auto-gen must run WITHOUT the modal progress dialog (else rapid clips
            # stack dialogs and freeze Anki).
            assert show_progress is False
            batch_calls.append([int(n) for n in note_ids])

    monkeypatch.setattr(gw, "BatchGenerator", _FakeBatch)

    col = _FakeCol(notes)

    # The gateway defers via QTimer.singleShot (NOT run_on_main, which runs synchronously on the
    # main thread — before note.id is set). Run the deferred closure inline so guards are testable.
    def _single_shot(_ms, fn):
        if run_deferred:
            fn()

    monkeypatch.setattr(aqt.qt.QTimer, "singleShot", staticmethod(_single_shot))

    mw = types.SimpleNamespace(col=col, taskman=types.SimpleNamespace())
    monkeypatch.setattr(aqt, "mw", mw)
    return col, batch_calls


def _gateway(settings):
    return IntegrationGateway(_service(), lambda: settings)


class TestIntegrationRegistry:
    def test_source_tag_resolves_its_integration(self):
        integration = integration_for_tags(["x", _SOURCE_TAG])
        assert integration is not None
        assert integration.key == "web_clipper"

    def test_unknown_tags_resolve_to_none(self):
        assert integration_for_tags(["nope", AUTOGEN_TAG]) is None


class TestConfigToggle:
    def test_disabled_by_default(self):
        assert SmartNotesSettings().integration_autogen_enabled("web_clipper") is False

    def test_enabled_when_set(self):
        settings = SmartNotesSettings(auto_generate_integrations={"web_clipper": True})
        assert settings.integration_autogen_enabled("web_clipper") is True


class TestGatewayGuards:
    def _note(self, tags, fields=None):
        return _FakeNote(1, "Basic", fields or {"Word": "cat", "Def": ""}, tags)

    def test_skips_when_autogen_tag_absent(self, monkeypatch):
        note = self._note([_SOURCE_TAG])  # source tag only, no caller opt-in
        _col, calls = _install(monkeypatch, {1: note})
        settings = SmartNotesSettings(
            note_types=[_note_type_config()],
            auto_generate_integrations={"web_clipper": True},
        )
        _gateway(settings).on_note_will_be_added(_col, note, 1)
        assert calls == []

    def test_skips_when_integration_toggle_off(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        _col, calls = _install(monkeypatch, {1: note})
        settings = SmartNotesSettings(
            note_types=[_note_type_config()]
        )  # auto_generate_integrations empty ⇒ OFF
        _gateway(settings).on_note_will_be_added(_col, note, 1)
        assert calls == []

    def test_skips_when_note_type_not_configured(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        _col, calls = _install(monkeypatch, {1: note})
        settings = SmartNotesSettings(
            note_types=[], auto_generate_integrations={"web_clipper": True}
        )
        _gateway(settings).on_note_will_be_added(_col, note, 1)
        assert calls == []

    def test_skips_when_no_empty_targets(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG], {"Word": "cat", "Def": "filled"})
        _col, calls = _install(monkeypatch, {1: note})
        settings = SmartNotesSettings(
            note_types=[_note_type_config()],
            auto_generate_integrations={"web_clipper": True},
        )
        _gateway(settings).on_note_will_be_added(_col, note, 1)
        assert calls == []

    def test_schedules_and_clears_tag_when_all_guards_pass(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        col, calls = _install(monkeypatch, {1: note})
        settings = SmartNotesSettings(
            note_types=[_note_type_config()],
            auto_generate_integrations={"web_clipper": True},
        )
        _gateway(settings).on_note_will_be_added(col, note, 1)
        assert calls == [[1]]  # BatchGenerator invoked for the single note id
        assert col.updated == [1]  # the tag removal was persisted via update_note
        assert not note.has_tag(AUTOGEN_TAG)  # one-shot caller tag cleared
        assert _SOURCE_TAG in note.tags  # source tag preserved

    def test_out_of_deck_scope_is_skipped(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        _col, calls = _install(monkeypatch, {1: note})
        config = _note_type_config().copy(update={"decks": [1]})
        settings = SmartNotesSettings(
            note_types=[config], auto_generate_integrations={"web_clipper": True}
        )
        # The note is being added to deck 9, but the config is scoped to deck 1 only.
        _gateway(settings).on_note_will_be_added(_col, note, 9)
        assert calls == []

    def test_failure_is_swallowed(self, monkeypatch):
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        _col, _calls = _install(monkeypatch, {1: note})

        def explode(_ms, _fn):
            raise RuntimeError("scheduling boom")

        import aqt

        monkeypatch.setattr(aqt.qt.QTimer, "singleShot", staticmethod(explode))
        settings = SmartNotesSettings(
            note_types=[_note_type_config()],
            auto_generate_integrations={"web_clipper": True},
        )
        # A thrown scheduling error must not propagate into the note-add path.
        _gateway(settings).on_note_will_be_added(_col, note, 1)

    def test_batch_dispatch_failure_does_not_wedge_gateway(self, monkeypatch):
        # If BatchGenerator.run raises synchronously (e.g. a queued note deleted mid-debounce),
        # the async `done` callback never fires — so _run_batch must reset _running itself, or the
        # gateway would be stuck (every future flush just re-arms) until Anki restarts.
        note = self._note([_SOURCE_TAG, AUTOGEN_TAG])
        col, _calls = _install(monkeypatch, {1: note})
        import omnia.plugins.smart_notes.integration.gateway as gw

        class _BoomBatch:
            def __init__(self, *_a):
                pass

            def run(self, *_a, **_k):
                raise RuntimeError("note vanished")

        monkeypatch.setattr(gw, "BatchGenerator", _BoomBatch)
        settings = SmartNotesSettings(
            note_types=[_note_type_config()],
            auto_generate_integrations={"web_clipper": True},
        )
        gateway = _gateway(settings)
        gateway.on_note_will_be_added(col, note, 1)  # inline timers run the whole chain
        assert (
            gateway._running is False
        )  # reset despite the synchronous failure — not wedged

    def test_registry_has_web_clipper(self):
        keys = {integration.key for integration in INTEGRATIONS}
        assert "web_clipper" in keys
