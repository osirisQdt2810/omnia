"""Tests for the regeneration glue: the service seam, per-field state, and the token.

Two rules drive almost every test here:

* **A lookup may not depend on smart_notes.** Turning Smart Notes off, or running a build that
  has no regeneration seam at all, must leave the search answering exactly as it did — only the
  regeneration flags go quiet.
* **A client is told what it may ask for.** ``can_regenerate`` and the per-field ``state`` are
  the whole reason a clipper can show a regenerate button that does not lie.

Nothing here needs smart_notes, Anki or a network: the seam is a stand-in injected into
``sys.modules`` and the collection reads are stubbed on ``anki_compat``.
"""

from __future__ import annotations

import os
import socket
import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import omnia.core
from omnia.core import anki_compat
from omnia.plugins.word_lookup import (
    REGENERATION_SERVICE,
    STATE_NO_RULE,
    STATE_UNAVAILABLE,
    RegenerationGateway,
    WordLookupPlugin,
    token_file_path,
    write_token_file,
)
from omnia.plugins.word_lookup.config import WEB_CLIPPER, WordLookupSettings
from omnia.plugins.word_lookup.service import (
    RegenerationDisabledError,
    RegenerationUnavailableError,
)

_NOTE_ID = 1234
_FIELDS = [("Word", "plunge"), ("Definition", "to dive"), ("Audio", "")]


@dataclass
class FakeOutcome:
    """Stands in for smart_notes' ``FieldOutcome`` (duck-typed on purpose)."""

    field: str
    status: str = "generated"
    message: str = ""
    text: str = ""


@dataclass
class FakeRegenerationService:
    """Stands in for what smart_notes publishes on ``smart_notes.regeneration``."""

    enabled: bool = True
    states: dict[str, str] = field(default_factory=dict)
    outcomes: list[FakeOutcome] = field(default_factory=list)
    # Set to an exception to make that call blow up — the degraded paths are the point.
    can_regenerate_error: Optional[Exception] = None
    field_states_error: Optional[Exception] = None
    calls: list[tuple[int, Any]] = field(default_factory=list)

    def can_regenerate(self) -> bool:
        if self.can_regenerate_error is not None:
            raise self.can_regenerate_error
        return self.enabled

    def field_states(self, note_id: int) -> dict[str, str]:
        if self.field_states_error is not None:
            raise self.field_states_error
        return dict(self.states)

    def regenerate(self, note_id: int, fields: Any) -> list[FakeOutcome]:
        self.calls.append((note_id, fields))
        return list(self.outcomes)


class FakeCard:
    """The one card a fake note has, with the scheduling fields the payload reads."""

    type = 2
    ivl = 10
    reps = 3
    lapses = 1
    did = 1


class FakeNote:
    """A note as word_lookup reads it: ordered fields, a note type name, and one card."""

    def __init__(
        self, fields: list[tuple[str, str]], note_type: str = "Vocabulary"
    ) -> None:
        self._fields = list(fields)
        self._note_type = note_type
        self.tags: list[str] = []

    def items(self) -> list[tuple[str, str]]:
        return list(self._fields)

    def note_type(self) -> dict[str, str]:
        return {"name": self._note_type}

    def cards(self) -> list[FakeCard]:
        return [FakeCard()]


def _free_port() -> int:
    """Return a port that is free right now (bind to 0 and read what the OS gave us)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def publish_regeneration(monkeypatch):
    """Publish a fake ``smart_notes.regeneration`` on the core service seam.

    ``core/services`` belongs to another feature and may not exist in the tree at all, so the
    module is injected rather than imported — the point of a seam is that word_lookup only ever
    sees a name and an object. Pass ``None`` for "the seam is there, smart_notes is not".
    """

    def publish(service: Any) -> Any:
        module = types.ModuleType("omnia.core.services")
        module.lookup = lambda name: service if name == REGENERATION_SERVICE else None
        monkeypatch.setitem(sys.modules, "omnia.core.services", module)
        monkeypatch.setattr(omnia.core, "services", module, raising=False)
        return service

    return publish


@pytest.fixture
def hide_regeneration(monkeypatch):
    """Make ``from omnia.core import services`` fail, as in a build without the seam.

    ``None`` in ``sys.modules`` is the interpreter's own "this import is halted" marker, so this
    reproduces the missing-module path exactly instead of approximating it.
    """
    monkeypatch.delattr(omnia.core, "services", raising=False)
    monkeypatch.setitem(sys.modules, "omnia.core.services", None)


@pytest.fixture
def plugin(monkeypatch):
    """A plugin wired to one fake note, with the collection reads stubbed out."""
    note = FakeNote(_FIELDS)
    monkeypatch.setattr(anki_compat, "find_note_ids", lambda query: [_NOTE_ID])
    monkeypatch.setattr(anki_compat, "get_note", lambda nid: note)
    monkeypatch.setattr(anki_compat, "get_note_or_none", lambda nid: note)
    instance = WordLookupPlugin()
    instance._ctx = SimpleNamespace(settings=WordLookupSettings())
    return instance


class TestWithoutTheSeam:
    """A build with no ``core/services``, and a build where smart_notes is simply off."""

    def test_a_missing_module_leaves_the_gateway_empty(self, hide_regeneration):
        gateway = RegenerationGateway.resolve()

        assert gateway.available is False
        assert gateway.can_regenerate() is False

    def test_a_service_that_is_not_published_leaves_it_empty_too(
        self, publish_regeneration
    ):
        publish_regeneration(None)

        assert RegenerationGateway.resolve().available is False

    def test_every_field_reports_unavailable(self, hide_regeneration):
        states = RegenerationGateway.resolve().field_states(1, ["A", "B"])

        assert states == {"A": STATE_UNAVAILABLE, "B": STATE_UNAVAILABLE}

    def test_the_search_is_unchanged(self, plugin, publish_regeneration):
        """The hard requirement: switching Smart Notes off may not degrade the lookup."""
        publish_regeneration(FakeRegenerationService(states={"Definition": "ready"}))
        with_service = plugin.lookup("plunge", WEB_CLIPPER)

        publish_regeneration(None)
        without_service = plugin.lookup("plunge", WEB_CLIPPER)

        assert without_service["found"] is True
        assert without_service["can_regenerate"] is False
        assert [f["name"] for f in without_service["cards"][0]["fields"]] == [
            f["name"] for f in with_service["cards"][0]["fields"]
        ]
        assert [f["text"] for f in without_service["cards"][0]["fields"]] == [
            f["text"] for f in with_service["cards"][0]["fields"]
        ]

    def test_a_seam_that_raises_is_not_a_failed_lookup(self, plugin, monkeypatch):
        def explode(_name):
            raise RuntimeError("the seam is broken")

        module = SimpleNamespace(lookup=explode)
        monkeypatch.setitem(sys.modules, "omnia.core.services", module)
        monkeypatch.setattr("omnia.core.services", module, raising=False)

        payload = plugin.lookup("plunge")

        assert payload["found"] is True and payload["can_regenerate"] is False


class TestPerFieldState:
    def test_the_reported_states_reach_the_fields(self, plugin, publish_regeneration):
        publish_regeneration(
            FakeRegenerationService(states={"Definition": "ready", "Audio": "blocked"})
        )

        payload = plugin.lookup("plunge", WEB_CLIPPER)

        states = {f["name"]: f["state"] for f in payload["cards"][0]["fields"]}
        assert states == {"Definition": "ready", "Audio": "blocked"}

    def test_a_field_smart_notes_did_not_mention_has_no_rule(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(FakeRegenerationService(states={"Definition": "ready"}))

        payload = plugin.lookup("plunge", WEB_CLIPPER)

        states = {f["name"]: f["state"] for f in payload["cards"][0]["fields"]}
        assert states["Audio"] == STATE_NO_RULE

    def test_can_regenerate_is_reported_at_the_top(self, plugin, publish_regeneration):
        publish_regeneration(FakeRegenerationService(enabled=False))

        assert plugin.lookup("plunge", WEB_CLIPPER)["can_regenerate"] is False

    def test_a_failing_field_states_call_does_not_fail_the_lookup(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(
            FakeRegenerationService(field_states_error=RuntimeError("boom"))
        )

        payload = plugin.lookup("plunge", WEB_CLIPPER)

        assert payload["found"] is True
        assert all(f["state"] == STATE_NO_RULE for f in payload["cards"][0]["fields"])

    def test_a_failing_can_regenerate_call_reads_as_off(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(
            FakeRegenerationService(can_regenerate_error=RuntimeError("boom"))
        )

        assert plugin.lookup("plunge", WEB_CLIPPER)["can_regenerate"] is False

    def test_an_empty_field_is_flagged_so_a_client_can_style_it(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(FakeRegenerationService(states={"Audio": "ready"}))

        fields = {f["name"]: f for f in plugin.lookup("plunge")["cards"][0]["fields"]}

        assert fields["Audio"]["empty"] is True
        assert fields["Definition"]["empty"] is False


class TestGenerating:
    def test_it_reports_the_stored_value_not_the_generated_one(
        self, plugin, publish_regeneration, monkeypatch
    ):
        """What the clipper renders must be what the note now holds, cleaned the usual way."""
        stored = FakeNote(
            [("Definition", "<b>to dive</b> [sound:d.mp3]"), ("Word", "plunge")]
        )
        monkeypatch.setattr(anki_compat, "get_note_or_none", lambda nid: stored)
        publish_regeneration(
            FakeRegenerationService(
                outcomes=[FakeOutcome("Definition", text="something else entirely")]
            )
        )

        payload = plugin.generate(WEB_CLIPPER, _NOTE_ID, ["Definition"])

        assert payload["note_id"] == _NOTE_ID
        assert payload["results"] == [
            {
                "field": "Definition",
                "status": "generated",
                "message": "",
                "text": "to dive",
                "audio": ["d.mp3"],
                "images": [],
            }
        ]

    def test_one_field_failing_does_not_stop_the_others(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(
            FakeRegenerationService(
                outcomes=[
                    FakeOutcome("Definition", status="generated"),
                    FakeOutcome("Audio", status="error", message="TTS refused"),
                    FakeOutcome("Word", status="skipped"),
                ]
            )
        )

        results = plugin.generate(WEB_CLIPPER, _NOTE_ID, None)["results"]

        assert [(r["field"], r["status"]) for r in results] == [
            ("Definition", "generated"),
            ("Audio", "error"),
            ("Word", "skipped"),
        ]
        assert results[1]["message"] == "TTS refused"

    def test_the_note_id_and_fields_are_passed_through(
        self, plugin, publish_regeneration
    ):
        service = publish_regeneration(FakeRegenerationService())

        plugin.generate(WEB_CLIPPER, _NOTE_ID, ["Audio"])

        assert service.calls == [(_NOTE_ID, ["Audio"])]

    def test_a_field_that_is_no_longer_on_the_note_falls_back_to_the_outcome(
        self, plugin, publish_regeneration
    ):
        publish_regeneration(
            FakeRegenerationService(
                outcomes=[FakeOutcome("Renamed", text="<i>generated</i>")]
            )
        )

        results = plugin.generate(WEB_CLIPPER, _NOTE_ID, ["Renamed"])["results"]

        assert results[0]["text"] == "generated"

    def test_a_deleted_note_still_answers(
        self, plugin, publish_regeneration, monkeypatch
    ):
        monkeypatch.setattr(anki_compat, "get_note_or_none", lambda nid: None)
        publish_regeneration(
            FakeRegenerationService(outcomes=[FakeOutcome("Definition")])
        )

        results = plugin.generate(WEB_CLIPPER, _NOTE_ID, None)["results"]

        assert results[0]["text"] == ""

    def test_smart_notes_off_raises_unavailable(self, plugin, hide_regeneration):
        with pytest.raises(RegenerationUnavailableError):
            plugin.generate(WEB_CLIPPER, _NOTE_ID, None)

    def test_the_option_being_off_raises_disabled_without_generating(
        self, plugin, publish_regeneration
    ):
        service = publish_regeneration(FakeRegenerationService(enabled=False))

        with pytest.raises(RegenerationDisabledError) as raised:
            plugin.generate(WEB_CLIPPER, _NOTE_ID, None)

        assert "Regenerate from clippers" in str(raised.value)
        assert service.calls == []

    def test_an_unexpected_failure_is_not_reported_as_disabled(
        self, plugin, publish_regeneration
    ):
        """A broken ``can_regenerate`` must surface as a 500, not as "you turned this off"."""
        publish_regeneration(
            FakeRegenerationService(can_regenerate_error=RuntimeError("boom"))
        )

        with pytest.raises(RuntimeError):
            plugin.generate(WEB_CLIPPER, _NOTE_ID, None)


class TestTheTokenFile:
    def test_it_holds_the_token(self, tmp_path):
        path = write_token_file(tmp_path, "s3cret")

        assert path == token_file_path(tmp_path)
        assert path.read_text(encoding="utf-8") == "s3cret"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_only_the_owner_can_read_it(self, tmp_path):
        path = write_token_file(tmp_path, "s3cret")

        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_rotated_token_tightens_a_file_that_was_already_wide(self, tmp_path):
        """``os.open`` only applies its mode when it CREATES the file."""
        path = token_file_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("old", encoding="utf-8")
        os.chmod(path, 0o666)

        write_token_file(tmp_path, "new")

        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text(encoding="utf-8") == "new"


class _FakeConfig:
    """Records what the plugin persisted (the repository's shallow-merge write)."""

    def __init__(self, fail: bool = False) -> None:
        self.writes: list[tuple[str, dict]] = []
        self._fail = fail

    def update_section(self, section: str, values: dict) -> None:
        if self._fail:
            raise OSError("config is read-only")
        self.writes.append((section, values))


class TestIssuingTheToken:
    @pytest.fixture
    def enabled(self, tmp_path):
        """Enable the plugin against a temp user_files, and always disable it again."""
        started: list[tuple[WordLookupPlugin, SimpleNamespace]] = []

        def enable(settings: WordLookupSettings, config=None) -> SimpleNamespace:
            ctx = SimpleNamespace(
                settings=settings.copy(update={"port": _free_port()}),
                config=config or _FakeConfig(),
                paths=SimpleNamespace(user_files_dir=tmp_path),
            )
            instance = WordLookupPlugin()
            instance.on_enable(ctx)
            started.append((instance, ctx))
            return ctx

        yield enable
        for instance, ctx in started:
            instance.on_disable(ctx)

    def test_a_first_enable_issues_persists_and_publishes_one(self, enabled, tmp_path):
        ctx = enabled(WordLookupSettings())

        section, values = ctx.config.writes[0]
        assert section == "word_lookup"
        token = values["token"]
        assert len(token) >= 32
        assert token_file_path(tmp_path).read_text(encoding="utf-8") == token

    def test_an_existing_token_is_reused_and_never_rewritten(self, enabled, tmp_path):
        ctx = enabled(WordLookupSettings.parse_obj({"token": "already-issued"}))

        assert ctx.config.writes == []
        assert token_file_path(tmp_path).read_text(encoding="utf-8") == "already-issued"

    def test_a_config_that_cannot_be_written_still_serves(self, enabled, tmp_path):
        """A read-only config must not cost the user the write path for this session."""
        enabled(WordLookupSettings(), config=_FakeConfig(fail=True))

        assert token_file_path(tmp_path).read_text(encoding="utf-8")

    def test_a_missing_user_files_path_is_not_a_crash(self):
        instance = WordLookupPlugin()
        ctx = SimpleNamespace(
            settings=WordLookupSettings(port=_free_port()),
            config=_FakeConfig(),
            paths=None,
        )

        instance.on_enable(ctx)
        instance.on_disable(ctx)

        assert ctx.config.writes, "the token was still issued and persisted"
