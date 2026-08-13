"""Tests for the extract_audio_file_name maintenance task (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import NoteView
from omnia.plugins.note_maintenance.tasks.extract_audio_file_name import (
    ExtractAudioFileNameConfig,
    ExtractAudioFileNameTask,
)


def _note(**fields: str) -> NoteView:
    return NoteView(note_id=7, note_type="Vocab", fields=dict(fields))


def _task(**fields: str) -> ExtractAudioFileNameTask:
    return ExtractAudioFileNameTask(ExtractAudioFileNameConfig(fields=dict(fields)))


class TestExtractAudioFileNameTask:
    def test_extracts_the_file_name(self):
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="[sound:plunge.mp3]", AudioNoTag="")
        assert task.process(note) == {"AudioNoTag": "plunge.mp3"}

    def test_ignores_surrounding_whitespace_and_tag_case(self):
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="  [SOUND:plunge.mp3] ", AudioNoTag="")
        assert task.process(note) == {"AudioNoTag": "plunge.mp3"}

    def test_no_change_when_the_field_is_not_only_a_sound_tag(self):
        task = _task(Audio="AudioNoTag")
        assert task.process(_note(Audio="listen: [sound:plunge.mp3]")) == {}

    def test_no_change_when_the_field_is_empty(self):
        assert _task(Audio="AudioNoTag").process(_note(Audio="")) == {}

    def test_no_change_when_target_already_holds_the_file_name(self):
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="[sound:plunge.mp3]", AudioNoTag="plunge.mp3")
        assert task.process(note) == {}

    def test_a_written_file_name_is_a_fixed_point(self):
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="[sound:rock &amp; roll.mp3]", AudioNoTag="")
        written = task.process(note)["AudioNoTag"]

        assert task.process(_note(Audio=note.field("Audio"), AudioNoTag=written)) == {}

    def test_handles_several_audio_fields(self):
        task = _task(WordAudio="WordAudioNoTag", ExampleAudio="ExampleAudioNoTag")
        note = _note(
            WordAudio="[sound:word.mp3]",
            ExampleAudio="[sound:example.ogg]",
        )
        assert task.process(note) == {
            "WordAudioNoTag": "word.mp3",
            "ExampleAudioNoTag": "example.ogg",
        }


class TestExtractAudioFileNameWritesFieldHtml:
    """The target is a stored-HTML field; a file name is plain text and has to be encoded."""

    def test_a_raw_ampersand_is_escaped(self):
        # A hand-written "[sound:a&b.mp3]" would otherwise leave a bare "&" in stored HTML,
        # where it reads as the start of an entity.
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="[sound:rock & roll.mp3]", AudioNoTag="")

        assert task.process(note) == {"AudioNoTag": "rock &amp; roll.mp3"}

    def test_an_encoded_reference_is_not_escaped_twice(self):
        # Anki decodes entities in a media reference, so this tag plays "rock & roll.mp3";
        # copying it verbatim (or escaping it again) would display "&amp;" to the user.
        task = _task(Audio="AudioNoTag")
        note = _note(Audio="[sound:rock &amp; roll.mp3]", AudioNoTag="")

        assert task.process(note) == {"AudioNoTag": "rock &amp; roll.mp3"}
