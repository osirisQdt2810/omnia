"""Tests for the regeneration capability Smart Notes publishes to other plugins.

Two things are pinned here that ordinary unit tests would let slide:

* **the status vocabulary.** Those seven strings cross an HTTP boundary into a clipper that
  ships separately, so each one gets a test that fails if its branch is removed — a rename is a
  protocol break, and a silently-missing branch is a button that never explains itself.
* **the threading contract.** ``_FakeCompat`` REFUSES every collection call that does not arrive
  inside ``run_on_main``, so any read or write this service forgets to marshal fails the whole
  file rather than only crashing inside a real Anki. The provider, conversely, records the hop
  depth it was called at and must be zero: an LLM round trip on the Qt main thread would freeze
  Anki for as long as the model takes.
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider

from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH
from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.integration import regen
from omnia.plugins.smart_notes.integration.regen import (
    REGENERATION_SERVICE,
    RegenerationService,
)


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


class _FakeCompat:
    """An ``anki_compat`` stand-in that enforces "collection calls run on the main thread".

    ``run_on_main`` runs its closure inline (there is no Qt loop) but raises the hop depth while
    it does, and every collection call asserts that depth is non-zero. A service that read a
    note straight from its worker thread would therefore fail loudly here instead of corrupting
    a real collection.
    """

    def __init__(self, notes, deck_ids=(1,)):
        self._notes = notes
        self._deck_ids = list(deck_ids)
        self.depth = 0
        self.hops = 0
        self.updated = []
        self.media = []

    def _require_main(self, what):
        if self.depth == 0:
            raise AssertionError(f"{what} touched the collection off the main thread")

    def run_on_main(self, callback):
        self.hops += 1
        self.depth += 1
        try:
            callback()
        finally:
            self.depth -= 1

    def get_note(self, nid, col=None):
        self._require_main("get_note")
        if nid not in self._notes:
            raise KeyError(f"no note {nid}")
        return self._notes[nid]

    def update_note(self, note, col=None):
        self._require_main("update_note")
        self.updated.append(note.id)

    def note_deck_ids(self, note, col=None):
        self._require_main("note_deck_ids")
        return list(self._deck_ids)

    def add_media_file(self, filename, data, col=None):
        self._require_main("add_media_file")
        self.media.append(filename)
        return filename


class _StubHub:
    def __init__(self, llm=None, tts=None):
        self._llm = llm
        self._tts = tts

    def llm(self, *, model="", provider="", image_model=""):
        if self._llm is None:
            raise AssertionError("no LLM in this test")
        return self._llm

    def tts(self, *, provider=""):
        if self._tts is None:
            raise AssertionError("no TTS in this test")
        return self._tts


class _RecordingLLM(FakeLLMProvider):
    """Records the main-thread hop depth each call was made at (must always be 0)."""

    def __init__(self, compat, text="generated"):
        super().__init__(text=text)
        self._compat = compat
        self.depths = []

    def generate_text(self, prompt, **kwargs):
        self.depths.append(self._compat.depth)
        return super().generate_text(prompt, **kwargs)


class _BrokenLLM(FakeLLMProvider):
    """Fails only for the prompts naming ``breaks_on``; generates normally for the rest."""

    def __init__(self, breaks_on, message="provider exploded"):
        super().__init__(text="generated")
        self._breaks_on = breaks_on
        self._message = message

    def generate_text(self, prompt, **kwargs):
        if self._breaks_on in prompt:
            raise ProviderError(self._message)
        return super().generate_text(prompt, **kwargs)


def _patch(monkeypatch, fake):
    for name in (
        "run_on_main",
        "get_note",
        "update_note",
        "note_deck_ids",
        "add_media_file",
    ):
        monkeypatch.setattr(regen.anki_compat, name, getattr(fake, name))


def _row(field, **overrides):
    values = {"field": field, "enabled": True, "type": "text"}
    values.update(overrides)
    return SmartNotesFieldConfig(**values)


def _config(fields=None, **overrides):
    values = {
        "note_type": "Vocab",
        "base_field": "Word",
        "fields": (
            fields
            if fields is not None
            else [
                _row("Definition", prompt="define {{Word}}"),
                _row("Example", prompt="use {{Word}}"),
            ]
        ),
    }
    values.update(overrides)
    return SmartNotesNoteTypeConfig(**values)


def _soft_dep_config():
    """A field whose only source is SOFT — the one shape that can reach the silent skip gate."""
    return _config(
        fields=[
            _row(
                "Definition",
                prompt="define {{Word}}",
                depends_on=[FieldDep(field="Word", kind="soft")],
            )
        ]
    )


def _soft_dep_note():
    return _FakeNote(1, "Vocab", {"Word": "", "Definition": ""})


def _settings(config=None, **overrides):
    return SmartNotesSettings(
        note_types=[config if config is not None else _config()], **overrides
    )


def _service(compat, llm=None, tts=None):
    return GenerationService(
        _StubHub(llm if llm is not None else _RecordingLLM(compat), tts),
        detect_tts_language=False,
    )


def _build(monkeypatch, note, settings, *, llm=None, tts=None, deck_ids=(1,)):
    """Wire a RegenerationService over a single fake note; returns ``(service, compat)``."""
    compat = _FakeCompat({note.id: note}, deck_ids=deck_ids)
    _patch(monkeypatch, compat)
    return RegenerationService(lambda: settings, _service(compat, llm, tts)), compat


def _statuses(outcomes):
    return {outcome.field: outcome.status for outcome in outcomes}


def _plugin_context():
    """The minimum PluginContext SmartNotesPlugin's enable/disable path actually reads."""
    import logging
    import tempfile
    from pathlib import Path

    from omnia.core.plugin import AddonPaths, PluginContext
    from omnia.core.providers import ProviderHub
    from omnia.core.reviewer.ease_pipeline import EasePipeline
    from omnia.core.reviewer.web_injector import WebInjector

    tmp = Path(tempfile.mkdtemp())
    return PluginContext(
        plugin_id="smart_notes",
        settings=SmartNotesSettings(),
        log=logging.getLogger("omnia.test"),
        ease=EasePipeline(),
        web=WebInjector(),
        providers=ProviderHub(),
        paths=AddonPaths(tmp, tmp, tmp),
        config=None,  # not read by the enable/disable path exercised here
        reload_self=lambda: None,
    )


class TestDispatch:
    def test_a_whole_note_regenerate_fans_out_rather_than_running_serially(
        self, monkeypatch
    ):
        # Ten fields is ten provider round trips with a person watching a spinner, and the
        # DAG's independent branches have no reason to wait on each other. Pinned explicitly
        # because dropping back to the default sequential dispatch changes no OUTCOME — every
        # other test in this file would still pass while the feature got several times slower.
        settings = _settings(max_concurrent_generations=4)
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(monkeypatch, note, settings)
        seen = {}
        inner = service._service.generate_note

        def spy(*args, **kwargs):
            seen["dispatch"] = kwargs.get("dispatch")
            return inner(*args, **kwargs)

        monkeypatch.setattr(service._service, "generate_note", spy)

        service.regenerate(1)

        assert seen["dispatch"] is not None
        assert seen["dispatch"] is not SEQUENTIAL_DISPATCH


class TestStatusVocabulary:
    def test_status_strings_are_the_wire_format(self):
        # These cross an HTTP boundary into a separately-shipped clipper: changing one is a
        # protocol break, so they are asserted literally rather than through the constants.
        assert regen.STATUS_GENERATED == "generated"
        assert regen.STATUS_SKIPPED == "skipped"
        assert regen.STATUS_BLOCKED == "blocked"
        assert regen.STATUS_ERROR == "error"
        assert regen.STATUS_NO_RULE == "no_rule"
        assert regen.STATUS_RULE_OFF == "rule_off"
        assert regen.STATUS_NOT_GENERATABLE == "not_generatable"
        assert regen.STATUS_READY == "ready"

    def test_service_name_is_the_published_name(self):
        assert REGENERATION_SERVICE == "smart_notes.regeneration"


class TestTheSetting:
    def test_defaults_on_and_is_always_serialized(self):
        # Deliberately NOT in _PRUNE_WHILE_UNSET: a plain bool costs nothing to sync, and a
        # clipper reading a blob written by another device must see the user's actual answer.
        settings = SmartNotesSettings()
        assert settings.regenerate_from_clippers is True
        assert settings.dict()["regenerate_from_clippers"] is True

    def test_survives_a_round_trip_through_the_collection_blob(self):
        stored = SmartNotesSettings(regenerate_from_clippers=False).dict()
        assert SmartNotesSettings.parse_obj(stored).regenerate_from_clippers is False

    def test_a_blob_from_before_the_setting_loads_with_the_default(self):
        # ADR-010: the blob syncs, so an older device's config must still validate.
        legacy = {"note_types": [], "regenerate_when_batching": False}
        assert SmartNotesSettings.parse_obj(legacy).regenerate_from_clippers is True


class TestCanRegenerate:
    def test_on_by_default(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(monkeypatch, note, _settings())
        assert service.can_regenerate() is True

    def test_off_when_the_setting_is_off(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(
            monkeypatch, note, _settings(regenerate_from_clippers=False)
        )
        assert service.can_regenerate() is False

    def test_off_without_settings(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat"})
        compat = _FakeCompat({1: note})
        _patch(monkeypatch, compat)
        service = RegenerationService(lambda: None, _service(compat))
        assert service.can_regenerate() is False

    def test_separate_switch_from_regenerate_when_batching(self, monkeypatch):
        # The user asked for its own option: turning batch regeneration off must not disarm the
        # clipper buttons, and vice versa.
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(
            monkeypatch, note, _settings(regenerate_when_batching=False)
        )
        assert service.can_regenerate() is True


class TestFieldStates:
    def test_reports_one_state_per_field_and_generates_nothing(self, monkeypatch):
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Definition": "old", "Example": "", "Notes": ""}
        )
        service, compat = _build(monkeypatch, note, _settings())
        states = service.field_states(1)
        assert states == {
            "Word": "no_rule",  # the base field is the input, never generated
            "Definition": "ready",
            "Example": "ready",
            "Notes": "no_rule",  # no row targets it
        }
        assert compat.updated == []
        assert note["Definition"] == "old"

    def test_rule_off_and_not_generatable_are_distinguished(self, monkeypatch):
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}", enabled=False),
                _row("Hologram", prompt="project {{Word}}", type="hologram"),
            ]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Hologram": ""})
        service, _ = _build(monkeypatch, note, _settings(config))
        states = service.field_states(1)
        assert states["Definition"] == "rule_off"
        assert states["Hologram"] == "not_generatable"

    def test_blocked_when_a_hard_prerequisite_is_empty(self, monkeypatch):
        # "Sentence" is an ordinary note field nothing generates, so an empty one can never be
        # filled by this run — the button must say so before the user pays for a round trip.
        config = _config(
            fields=[_row("Example", prompt="use {{Word}} like {{Sentence}}")]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Sentence": "", "Example": ""})
        service, _ = _build(monkeypatch, note, _settings(config))
        assert service.field_states(1)["Example"] == "blocked"

    def test_ready_when_the_hard_prerequisite_is_filled(self, monkeypatch):
        config = _config(
            fields=[_row("Example", prompt="use {{Word}} like {{Sentence}}")]
        )
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Sentence": "a cat sat", "Example": ""}
        )
        service, _ = _build(monkeypatch, note, _settings(config))
        assert service.field_states(1)["Example"] == "ready"

    def test_unreadable_note_degrades_to_an_empty_map(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat"})
        service, _ = _build(monkeypatch, note, _settings())
        assert (
            service.field_states(999) == {}
        )  # must not raise: the UI cannot act on that

    def test_note_type_without_config_has_no_rules(self, monkeypatch):
        note = _FakeNote(1, "Other", {"Front": "cat", "Back": ""})
        service, _ = _build(monkeypatch, note, _settings())
        assert service.field_states(1) == {"Front": "no_rule", "Back": "no_rule"}


class TestRegenerateGenerated:
    def test_generates_every_field_and_writes_the_note(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, compat = _build(monkeypatch, note, _settings())
        outcomes = service.regenerate(1)
        # One status per field of the NOTE — the base field included, saying why it is not
        # generated. See TestAWholeNoteRequestAnswersForEveryField for why that matters.
        assert _statuses(outcomes) == {
            "Word": "no_rule",
            "Definition": "generated",
            "Example": "generated",
        }
        generated = [o for o in outcomes if o.status == "generated"]
        assert [outcome.text for outcome in generated] == ["generated", "generated"]
        assert [outcome.message for outcome in generated] == ["", ""]
        assert note["Definition"] == "generated"
        assert compat.updated == [1]

    def test_always_overwrites_an_already_filled_field(self, monkeypatch):
        # The per-field ``overwrite`` flag is off (the default) and the target is full: an
        # automatic batch would leave it alone, but "regenerate" means make it again.
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Definition": "stale", "Example": "stale"}
        )
        service, _ = _build(monkeypatch, note, _settings())
        assert _statuses(service.regenerate(1)) == {
            "Word": "no_rule",
            "Definition": "generated",
            "Example": "generated",
        }
        assert note["Definition"] == "generated"

    def test_explicit_fields_touch_only_those(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": "e"})
        service, _ = _build(monkeypatch, note, _settings())
        outcomes = service.regenerate(1, ["Definition"])
        assert [outcome.field for outcome in outcomes] == ["Definition"]
        assert note["Example"] == "e"

    def test_outcomes_come_back_in_the_requested_order(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, _ = _build(monkeypatch, note, _settings())
        outcomes = service.regenerate(1, ["Example", "Word", "Definition"])
        assert [outcome.field for outcome in outcomes] == [
            "Example",
            "Word",
            "Definition",
        ]

    def test_media_bytes_are_added_exactly_once(self, monkeypatch):
        # The chain needs the [sound:…] reference DURING the run and the write-back needs it
        # after; a second add_media_file would store a renamed duplicate the note never points at.
        config = _config(
            fields=[_row("Audio", type="tts", prompt="{{Word}}", voice="alloy")]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Audio": ""})
        service, compat = _build(
            monkeypatch, note, _settings(config), tts=FakeTTSProvider()
        )
        outcomes = {outcome.field: outcome for outcome in service.regenerate(1)}
        assert compat.media == ["omnia-1-Audio.mp3"]
        assert note["Audio"] == "[sound:omnia-1-Audio.mp3]"
        assert outcomes["Audio"].text == "[sound:omnia-1-Audio.mp3]"


class TestRegenerateHonoursTheEnabledCheckbox:
    """The one place this deliberately differs from ``single_field_config``.

    That helper forces ``enabled=True`` so the editor's right-click menu can generate a field
    the user disabled for batching — correct there, because the user is looking at the field and
    just asked for it. A clipper is not that, so reusing it here would silently generate a field
    whose Generate box was deliberately unticked. Delete the ``rule_off`` branch (or swap the
    sub-config for ``single_field_config``) and these two tests fail.
    """

    def test_a_disabled_field_is_refused_not_generated(self, monkeypatch):
        config = _config(
            fields=[_row("Definition", prompt="define {{Word}}", enabled=False)]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, compat = _build(monkeypatch, note, _settings(config))
        outcome = service.regenerate(1, ["Definition"])[0]
        assert outcome.status == "rule_off"
        assert "tick it" in outcome.message
        assert note["Definition"] == ""  # nothing was generated into it
        assert compat.updated == []  # and the note was never written

    def test_a_disabled_field_does_not_stop_its_enabled_siblings(self, monkeypatch):
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}", enabled=False),
                _row("Example", prompt="use {{Word}}"),
            ]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, _ = _build(monkeypatch, note, _settings(config))
        assert _statuses(service.regenerate(1, ["Definition", "Example"])) == {
            "Definition": "rule_off",
            "Example": "generated",
        }
        assert note["Definition"] == ""
        assert note["Example"] == "generated"

    def test_regenerate_all_reports_a_disabled_field_rather_than_dropping_it(
        self, monkeypatch
    ):
        # It is still not generated — that is the rule this class protects. What changed is
        # that a whole-note request now SAYS so: the order used to be built from
        # ``generatable_fields()``, which filters ``enabled`` first, so the one field the user
        # needed an explanation for was the one silently missing from the answer.
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}", enabled=False),
                _row("Example", prompt="use {{Word}}"),
            ]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, _ = _build(monkeypatch, note, _settings(config))
        assert _statuses(service.regenerate(1)) == {
            "Word": "no_rule",
            "Definition": "rule_off",
            "Example": "generated",
        }
        assert note["Definition"] == ""


class TestAWholeNoteRequestAnswersForEveryField:
    """ "Generate all" must report on the same fields ``field_states`` previewed — or say nothing.

    The order for ``fields=None`` used to be derived from the CONFIG, and both ways it could be
    empty while ``field_states`` reported on every field of the note: no config at all gave
    ``[]``, and a config with every Generate box unticked gave ``[]`` too, because
    ``generatable_fields()`` filters ``enabled`` before the order is built. A clipper then got
    ``200 {"results": []}`` for a button it had just been told to show — nothing rendered, and
    nothing said. Revert the note-derived order in ``_classify`` and every test here fails.
    """

    def test_an_unconfigured_note_type_answers_for_every_field(self, monkeypatch):
        note = _FakeNote(1, "Other", {"Front": "cat", "Back": ""})
        service, _ = _build(monkeypatch, note, _settings())
        outcomes = service.regenerate(1)
        assert _statuses(outcomes) == {"Front": "no_rule", "Back": "no_rule"}
        assert "no Smart Notes configuration" in outcomes[0].message

    def test_a_config_with_every_row_disabled_answers_rule_off(self, monkeypatch):
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}", enabled=False),
                _row("Example", prompt="use {{Word}}", enabled=False),
            ]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, compat = _build(monkeypatch, note, _settings(config))
        outcomes = {outcome.field: outcome for outcome in service.regenerate(1)}
        assert _statuses(outcomes.values()) == {
            "Word": "no_rule",
            "Definition": "rule_off",
            "Example": "rule_off",
        }
        assert "tick it" in outcomes["Definition"].message
        assert compat.updated == []

    def test_it_reports_on_exactly_what_field_states_previewed(self, monkeypatch):
        # The two are one promise, so they are pinned against each other rather than against a
        # literal: whatever the panel drew a button for, the run has an answer for.
        note = _FakeNote(1, "Other", {"Front": "cat", "Back": "", "Extra": ""})
        service, _ = _build(monkeypatch, note, _settings())
        assert [outcome.field for outcome in service.regenerate(1)] == list(
            service.field_states(1)
        )


class TestRegenerateRefusals:
    def test_no_rule_for_the_base_field_and_for_an_unconfigured_field(
        self, monkeypatch
    ):
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Definition": "", "Example": "", "Notes": ""}
        )
        service, _ = _build(monkeypatch, note, _settings())
        outcomes = service.regenerate(1, ["Word", "Notes"])
        assert _statuses(outcomes) == {"Word": "no_rule", "Notes": "no_rule"}
        assert "input field" in outcomes[0].message

    def test_no_rule_for_a_field_the_note_does_not_have(self, monkeypatch):
        # The request arrives over HTTP from a UI that may be looking at a stale note type.
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(monkeypatch, note, _settings())
        outcome = service.regenerate(1, ["Ghost"])[0]
        assert outcome.status == "no_rule"
        assert "no field named" in outcome.message

    def test_not_generatable_for_a_type_this_build_cannot_make(self, monkeypatch):
        config = _config(
            fields=[_row("Hologram", type="hologram", prompt="project {{Word}}")]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Hologram": ""})
        service, compat = _build(monkeypatch, note, _settings(config))
        outcome = service.regenerate(1, ["Hologram"])[0]
        assert outcome.status == "not_generatable"
        assert "update Omnia" in outcome.message
        assert compat.updated == []

    def test_blocked_when_the_master_switch_is_off(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, compat = _build(
            monkeypatch, note, _settings(regenerate_from_clippers=False)
        )
        outcomes = {outcome.field: outcome for outcome in service.regenerate(1)}
        assert _statuses(outcomes.values()) == {
            "Word": "no_rule",  # the switch does not relabel a field that has no rule
            "Definition": "blocked",
            "Example": "blocked",
        }
        assert "switched off" in outcomes["Definition"].message
        assert compat.updated == []

    def test_the_switch_does_not_relabel_a_field_that_has_no_rule(self, monkeypatch):
        # Sending the user to a setting that would not help them is worse than saying nothing.
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, _ = _build(
            monkeypatch, note, _settings(regenerate_from_clippers=False)
        )
        assert service.regenerate(1, ["Word"])[0].status == "no_rule"

    def test_blocked_when_the_note_is_outside_the_config_deck_scope(self, monkeypatch):
        config = _config(decks=[7])
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        service, compat = _build(monkeypatch, note, _settings(config), deck_ids=(1, 2))
        outcome = service.regenerate(1, ["Definition"])[0]
        assert outcome.status == "blocked"
        assert "deck scope" in outcome.message
        assert compat.updated == []

    def test_no_rule_when_the_note_type_has_no_config(self, monkeypatch):
        note = _FakeNote(1, "Other", {"Front": "cat", "Back": ""})
        service, _ = _build(monkeypatch, note, _settings())
        outcome = service.regenerate(1, ["Back"])[0]
        assert outcome.status == "no_rule"
        assert "no Smart Notes configuration" in outcome.message


class TestRegenerateRunOutcomes:
    def test_blocked_names_the_empty_hard_prerequisite(self, monkeypatch):
        config = _config(
            fields=[_row("Example", prompt="use {{Word}} like {{Sentence}}")]
        )
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Sentence": "", "Example": ""})
        service, _ = _build(monkeypatch, note, _settings(config))
        outcome = service.regenerate(1, ["Example"])[0]
        assert outcome.status == "blocked"
        assert "Sentence" in outcome.message

    def test_skipped_names_the_blank_source_field(self, monkeypatch):
        # The silent gate: every referenced source is blank and allow_empty_fields is off, so
        # the engine drops the field without reporting it ANYWHERE. Derived, not signalled.
        #
        # The dep must be SOFT to reach it: a blank HARD prerequisite is caught one gate earlier
        # and reported as blocked, so ``should_skip_rule``'s empty-sources branch is only ever
        # the last word for a field that explicitly said "order me after this, but don't wait
        # for it".
        service, compat = _build(
            monkeypatch, _soft_dep_note(), _settings(_soft_dep_config())
        )
        outcome = service.regenerate(1, ["Definition"])[0]
        assert outcome.status == "skipped"
        assert "Word" in outcome.message
        assert "empty" in outcome.message
        assert compat.updated == []

    def test_allow_empty_fields_turns_a_skip_into_a_generation(self, monkeypatch):
        service, _ = _build(
            monkeypatch,
            _soft_dep_note(),
            _settings(_soft_dep_config(), allow_empty_fields=True),
        )
        assert service.regenerate(1, ["Definition"])[0].status == "generated"

    def test_error_carries_the_chain_summary(self, monkeypatch):
        config = _config(fields=[_row("Definition", prompt="define {{Word}}")])
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        service, compat = _build(
            monkeypatch,
            note,
            _settings(config),
            llm=_BrokenLLM("define", "HTTP 401"),
        )
        outcome = service.regenerate(1, ["Definition"])[0]
        assert outcome.status == "error"
        assert outcome.message == "HTTP 401"
        assert compat.updated == []

    def test_one_failing_field_does_not_stop_the_others(self, monkeypatch):
        # "gen all" must run to the end and report what broke, not abort at the first failure.
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}"),
                _row("Example", prompt="use {{Word}}"),
                _row("Synonyms", prompt="synonyms of {{Word}}"),
            ]
        )
        note = _FakeNote(
            1,
            "Vocab",
            {"Word": "cat", "Definition": "", "Example": "", "Synonyms": ""},
        )
        service, _ = _build(
            monkeypatch, note, _settings(config), llm=_BrokenLLM("use", "HTTP 500")
        )
        assert _statuses(service.regenerate(1)) == {
            "Word": "no_rule",
            "Definition": "generated",
            "Example": "error",
            "Synonyms": "generated",
        }
        assert note["Synonyms"] == "generated"


class TestRegenerateDependencies:
    def test_a_dependent_field_reads_its_freshly_generated_prerequisite(
        self, monkeypatch
    ):
        # Both requested: the DAG runs Definition first and Example interpolates its new value.
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}"),
                _row("Example", prompt="use {{Definition}}"),
            ]
        )
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Definition": "stale", "Example": ""}
        )
        llm = FakeLLMProvider(text="ok")
        seen: list[str] = []
        llm.generate_text = lambda prompt, **kw: (seen.append(prompt), "fresh")[1]
        service, _ = _build(monkeypatch, note, _settings(config), llm=llm)
        assert _statuses(service.regenerate(1)) == {
            "Word": "no_rule",
            "Definition": "generated",
            "Example": "generated",
        }
        assert "use fresh" in seen  # not "use stale"

    def test_a_field_asked_for_alone_reads_the_note_as_it_stands(self, monkeypatch):
        config = _config(
            fields=[
                _row("Definition", prompt="define {{Word}}"),
                _row("Example", prompt="use {{Definition}}"),
            ]
        )
        note = _FakeNote(
            1, "Vocab", {"Word": "cat", "Definition": "stale", "Example": ""}
        )
        llm = FakeLLMProvider(text="ok")
        seen: list[str] = []
        llm.generate_text = lambda prompt, **kw: (seen.append(prompt), "fresh")[1]
        service, _ = _build(monkeypatch, note, _settings(config), llm=llm)
        assert service.regenerate(1, ["Example"])[0].status == "generated"
        assert seen == ["use stale"]
        assert note["Definition"] == "stale"


class TestPluginPublishesTheCapability:
    """The mechanism behind "Smart Notes off ⇒ search still works, generation does not"."""

    def test_enable_publishes_and_disable_withdraws(self):
        from omnia.core import services
        from omnia.plugins.smart_notes import SmartNotesPlugin

        assert services.lookup(REGENERATION_SERVICE) is None
        plugin = SmartNotesPlugin()
        ctx = _plugin_context()
        try:
            plugin.on_enable(ctx)
            published = services.lookup(REGENERATION_SERVICE)
            assert isinstance(published, RegenerationService)
            plugin.on_disable(ctx)
            # A consumer holds no import of this plugin, only a name — and it stops resolving.
            assert services.lookup(REGENERATION_SERVICE) is None
        finally:
            services.revoke(REGENERATION_SERVICE)

    def test_re_enable_publishes_the_new_instance(self):
        from omnia.core import services
        from omnia.plugins.smart_notes import SmartNotesPlugin

        plugin = SmartNotesPlugin()
        ctx = _plugin_context()
        try:
            plugin.on_enable(ctx)
            first = services.lookup(REGENERATION_SERVICE)
            plugin.on_disable(ctx)
            plugin.on_enable(ctx)
            second = services.lookup(REGENERATION_SERVICE)
            assert second is not first
        finally:
            plugin.on_disable(ctx)
            services.revoke(REGENERATION_SERVICE)

    # The next two are ONE check, split across the boundary they are about: a registry entry
    # must not outlive the test that made it. Nothing else can express that — the subject is
    # what happens BETWEEN two tests — so they run in definition order (pytest's default; this
    # suite has no shuffling plugin) and the second is the assertion.
    def test_a_publication_that_is_never_revoked_still_resolves(self):
        # What an ``on_enable`` whose ``on_disable`` is never reached leaves behind — the two
        # tests above only stay clean because each one revokes in a ``finally``.
        from omnia.core import services

        services.provide(REGENERATION_SERVICE, object())

        assert services.lookup(REGENERATION_SERVICE) is not None

    def test_and_the_next_test_finds_the_registry_clean(self):
        # ADR-019 promised "a fixture that clears it"; the only one lived in
        # tests/core/test_services.py, i.e. the single file no other test could dirty. Without
        # the autouse fixture in tests/conftest.py the publication above is still resolvable
        # here — and in every later test asserting a consumer degrades when NOBODY provides.
        from omnia.core import services

        assert services.lookup(REGENERATION_SERVICE) is None


class TestThreading:
    def test_collection_work_hops_to_the_main_thread_and_providers_do_not(
        self, monkeypatch
    ):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": "", "Example": ""})
        compat = _FakeCompat({1: note})
        _patch(monkeypatch, compat)
        llm = _RecordingLLM(compat)
        service = RegenerationService(
            lambda: _settings(), GenerationService(_StubHub(llm))
        )
        service.regenerate(1)
        # The read and the write-back each took a hop (_FakeCompat asserts the rest).
        assert compat.hops >= 2
        # The expensive part stayed off it: an LLM call inside run_on_main would freeze Anki.
        assert llm.depths == [0, 0]

    def test_a_wedged_main_thread_raises_instead_of_hanging(self, monkeypatch):
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        compat = _FakeCompat({1: note})
        _patch(monkeypatch, compat)
        monkeypatch.setattr(regen.anki_compat, "run_on_main", lambda callback: None)
        monkeypatch.setattr(regen, "_MAIN_THREAD_TIMEOUT_SECONDS", 0.05)
        service = RegenerationService(lambda: _settings(), _service(compat))
        with pytest.raises(TimeoutError):
            service.regenerate(1)
        # field_states swallows it, because a UI cannot render an exception.
        assert service.field_states(1) == {}


class TestOneRowIsOneRoundTrip:
    """Every candidate becomes a rule, and every rule is a provider call on a paid key."""

    def test_a_field_named_twice_is_generated_once(self, monkeypatch):
        settings = _settings()
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        compat = _FakeCompat({note.id: note})
        _patch(monkeypatch, compat)
        llm = _RecordingLLM(compat)
        service = RegenerationService(lambda: settings, _service(compat, llm))

        outcomes = service.regenerate(1, ["Definition", "Definition"])

        # The caller asked twice, so it hears twice — but the model is only asked once.
        assert [o.field for o in outcomes] == ["Definition", "Definition"]
        assert len(llm.depths) == 1

    def test_fifty_repeats_are_still_one_round_trip(self, monkeypatch):
        # The body cap admits thousands of names. Without the dedupe one authenticated request
        # buys thousands of identical generations — and a retry loop in either separately
        # shipped clipper produces exactly that request without anyone meaning to.
        settings = _settings()
        note = _FakeNote(1, "Vocab", {"Word": "cat", "Definition": ""})
        compat = _FakeCompat({note.id: note})
        _patch(monkeypatch, compat)
        llm = _RecordingLLM(compat)
        service = RegenerationService(lambda: settings, _service(compat, llm))

        outcomes = service.regenerate(1, ["Definition"] * 50)

        assert len(outcomes) == 50
        assert {o.status for o in outcomes} == {regen.STATUS_GENERATED}
        assert len(llm.depths) == 1


class TestWhoIsBlamedForASkip:
    """A blank source is the usual reason a field is skipped — not always the real one."""

    def test_a_source_that_failed_is_named_instead_of_being_called_empty(
        self, monkeypatch
    ):
        # Word is generated first and fails; Definition reads it SOFTLY, so it passes the block
        # gate and reaches the silent skip gate with Word still empty. Telling the user to fill
        # Word, or to turn on "generate even when sources are empty", points away from the
        # actual cause and the second suggestion would change nothing at all.
        config = _config(
            base_field="Base",
            fields=[
                _row("Word", prompt="the word"),
                _row(
                    "Definition",
                    prompt="define {{Word}}",
                    depends_on=[FieldDep(field="Word", kind="soft")],
                ),
            ],
        )
        note = _FakeNote(1, "Vocab", {"Base": "seed", "Word": "", "Definition": ""})
        compat = _FakeCompat({note.id: note})
        _patch(monkeypatch, compat)
        service = RegenerationService(
            lambda: _settings(config),
            _service(compat, _BrokenLLM("the word", "HTTP 401")),
        )

        outcomes = {o.field: o for o in service.regenerate(1)}

        assert outcomes["Word"].status == regen.STATUS_ERROR
        assert outcomes["Definition"].status == regen.STATUS_SKIPPED
        assert "Word" in outcomes["Definition"].message
        assert "did not generate" in outcomes["Definition"].message
        assert "empty" not in outcomes["Definition"].message

    def test_a_genuinely_blank_source_is_still_named_as_blank(self, monkeypatch):
        service, _ = _build(
            monkeypatch, _soft_dep_note(), _settings(_soft_dep_config())
        )

        outcome = service.regenerate(1, ["Definition"])[0]

        assert outcome.status == regen.STATUS_SKIPPED
        assert "empty" in outcome.message
