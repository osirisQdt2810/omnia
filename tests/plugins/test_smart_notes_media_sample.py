"""Tests for the Try-it media sample: the reference form, and the staging lifecycle.

Both are pure logic in a headless module (no ``aqt``/``anki``), so they are exercised directly
rather than only through the dialog controller — which is how the ``<img>`` branch and the
stage's own edge cases went uncovered.
"""

from __future__ import annotations

from pathlib import Path

from omnia.plugins.smart_notes.engine.tools.media_sample import (
    MediaSampleStage,
    media_reference,
)


class TestMediaReference:
    """How a note refers to a file, decided by extension rather than by media type.

    Anki has exactly two forms — ``<img>`` for pictures, ``[sound:…]`` for everything it plays,
    video included — so the split follows Anki's own behaviour instead of a list of the formats
    this feature happened to be asked about first.
    """

    def test_pictures_use_an_img_tag(self):
        for name in ("pic.png", "photo.jpg", "scan.jpeg", "anim.gif", "art.webp"):
            assert media_reference(name) == f'<img src="{name}">', name

    def test_everything_else_uses_a_sound_tag(self):
        # Including VIDEO: Anki plays mp4 through the same [sound:] reference.
        for name in ("voice.mp3", "clip.mp4", "take.wav", "movie.mkv"):
            assert media_reference(name) == f"[sound:{name}]", name

    def test_the_extension_check_ignores_case(self):
        assert media_reference("SCAN.JPG") == '<img src="SCAN.JPG">'

    def test_an_unknown_or_missing_extension_falls_back_to_sound(self):
        # What Anki itself does with something it cannot classify.
        assert media_reference("notes.xyz") == "[sound:notes.xyz]"
        assert media_reference("noextension") == "[sound:noextension]"


class TestMediaSampleStage:
    """Where a Try-it sample lives: outside the collection, one file, cleaned up after.

    The collection is deliberately not involved — Anki syncs media, so staging a test sample
    there would push it to every device and its removal would push a deletion.
    """

    def test_nothing_is_created_until_something_is_staged(self, tmp_path):
        # A session that never picks a file must leave no temp directory behind.
        assert MediaSampleStage().directory == ""

    def test_staging_copies_the_file_and_returns_its_name(self, tmp_path):
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"payload")
        stage = MediaSampleStage()

        name = stage.stage(source)

        assert name == "clip.mp4"
        assert (Path(stage.directory) / name).read_bytes() == b"payload"
        assert source.exists()  # a COPY — the original is untouched

    def test_staging_again_removes_the_previous_file(self, tmp_path):
        # Otherwise a session accumulates a copy of everything browsed through.
        first, second = tmp_path / "a.mp3", tmp_path / "b.mp3"
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        stage = MediaSampleStage()
        stage.stage(first)
        staged_first = Path(stage.directory) / "a.mp3"

        stage.stage(second)

        assert not staged_first.exists()
        assert (Path(stage.directory) / "b.mp3").exists()

    def test_clearing_nothing_is_safe(self):
        stage = MediaSampleStage()

        stage.clear()  # must not raise on a stage that has never been used

        assert stage.directory == ""

    def test_dispose_removes_a_folder_it_created(self, tmp_path):
        source = tmp_path / "c.wav"
        source.write_bytes(b"x")
        stage = MediaSampleStage()
        stage.stage(source)
        created = Path(stage.directory)

        stage.dispose()

        assert not created.exists()
        assert stage.directory == ""

    def test_dispose_leaves_a_borrowed_folder_alone(self, tmp_path):
        """A root passed in belongs to the caller.

        Quietly rmtree-ing a directory this object did not create is exactly the class of thing
        it exists to avoid doing — and today only tests inject one, which is precisely when a
        latent `rmtree` is least likely to be noticed.
        """
        borrowed = tmp_path / "borrowed"
        borrowed.mkdir()
        keeper = borrowed / "not-ours.txt"
        keeper.write_text("keep me")
        source = tmp_path / "d.mp3"
        source.write_bytes(b"x")
        stage = MediaSampleStage(root=borrowed)
        stage.stage(source)

        stage.dispose()

        assert borrowed.exists()
        assert keeper.read_text() == "keep me"

    def test_a_borrowed_root_still_reports_empty_until_staged(self, tmp_path):
        # The folder exists from construction; the media reference a tool is about to resolve
        # is certainly not in it yet, so reporting the folder would be a lie a tool acts on.
        borrowed = tmp_path / "borrowed"
        borrowed.mkdir()

        assert MediaSampleStage(root=borrowed).directory == ""
