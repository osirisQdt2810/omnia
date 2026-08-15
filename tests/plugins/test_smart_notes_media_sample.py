"""Tests for the Try-it media sample: the reference form, the classifiers, and the staging.

All of it is pure logic in a headless module (no ``aqt``/``anki``), so it is exercised directly
rather than only through the dialog controller — which is how the ``<img>`` branch and the
stage's own edge cases went uncovered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnia.plugins.smart_notes.engine.tools import INPUT_KIND_EXTENSIONS
from omnia.plugins.smart_notes.engine.tools.media_sample import (
    MediaSampleStage,
    image_data_uri,
    media_family,
    media_reference,
)

#: The IANA image types a picture may legitimately be announced as. Written out HERE, in the
#: test, so the assertion below is an independent statement about what Chromium will accept
#: rather than a re-read of the table it is checking.
_REAL_IMAGE_MIME = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/tiff",
        "image/webp",
    }
)


class TestOneExtensionVocabulary:
    """The picker's filter and the classifiers answer one question, from one table.

    They were written out three times and the copies drifted: ``INPUT_KIND_EXTENSIONS['image']``
    listed six extensions while the classifier called ten of them pictures, so the picker that
    exists to find a scan hid every ``.bmp`` and ``.tiff`` in the folder. Everything is derived
    from the one table now, and these are what keep it that way.
    """

    def test_every_offered_image_extension_is_classified_as_a_picture(self):
        for extension in INPUT_KIND_EXTENSIONS["image"]:
            assert media_family(extension) == "image", extension
            assert media_reference(f"scan.{extension}").startswith("<img"), extension

    def test_every_offered_video_extension_is_classified_as_a_video(self):
        for extension in INPUT_KIND_EXTENSIONS["video"]:
            assert media_family(extension) == "video", extension

    def test_every_offered_image_extension_inlines_under_a_real_mime_type(self):
        # The MIME map holds only the types that are NOT `image/<ext>`, so it is not a second
        # copy of the vocabulary — but a format added to the table must still land on a type
        # Chromium accepts, rather than a plausible-looking `image/tif` it refuses.
        for extension in INPUT_KIND_EXTENSIONS["image"]:
            mime = image_data_uri(b"x", extension).split(";", 1)[0].split(":", 1)[1]

            assert mime in _REAL_IMAGE_MIME, extension


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


class TestMediaFamily:
    """Which player a file needs, so the Try-it output can be rendered as what it is."""

    def test_pictures_are_images(self):
        for name in ("pic.png", "photo.jpg", "art.webp", "scan.tiff"):
            assert media_family(name) == "image", name

    def test_containers_anki_plays_with_a_picture_are_video(self):
        for name in ("clip.mp4", "take.webm", "reel.mov", "old.avi"):
            assert media_family(name) == "video", name

    def test_sound_files_are_audio(self):
        for name in ("voice.mp3", "take.wav", "note.m4a", "raw.flac"):
            assert media_family(name) == "audio", name

    def test_an_unknown_or_missing_extension_falls_back_to_audio(self):
        # The same "Anki plays it" fallback media_reference takes: hand it to the player rather
        # than declare a file this build has never heard of unrenderable.
        assert media_family("notes.xyz") == "audio"
        assert media_family("noextension") == "audio"
        assert media_family("") == "audio"

    def test_a_bare_extension_is_accepted_too(self):
        # The controller classifies a PRODUCED file, which has an extension and no name.
        assert media_family("mp4") == "video"
        assert media_family(".png") == "image"

    def test_the_check_ignores_case(self):
        assert media_family("SCAN.JPG") == "image"
        assert media_family("CLIP.MP4") == "video"


class TestImageDataUri:
    """The MIME an inlined picture is announced under.

    Written out rather than taken from ``mimetypes``, because the stdlib table answers types
    Chromium refuses on the content type alone — which makes a perfectly good file look broken.
    """

    def test_jpg_is_announced_as_the_canonical_jpeg_type(self):
        # `data:image/jpg` is not a real MIME type; it is what f"image/{ext}" produces.
        assert image_data_uri(b"\xff\xd8", "jpg").startswith("data:image/jpeg;base64,")
        assert image_data_uri(b"\xff\xd8", "jpeg").startswith("data:image/jpeg;base64,")

    def test_the_bytes_are_carried_base64_encoded(self):
        assert image_data_uri(b"hello", "png") == "data:image/png;base64,aGVsbG8="

    def test_an_unlisted_extension_falls_back_to_its_own_name(self):
        assert image_data_uri(b"x", "heic").startswith("data:image/heic;base64,")
        assert image_data_uri(b"x", "").startswith("data:image/png;base64,")


class TestMediaSampleStage:
    """Where a Try-it sample lives: outside the collection, one file per input, cleaned up after.

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

        name = stage.stage(source, slot="Clip")

        assert name == "clip.mp4"
        assert (Path(stage.directory) / name).read_bytes() == b"payload"
        assert source.exists()  # a COPY — the original is untouched

    def test_staging_again_removes_the_previous_file(self, tmp_path):
        # Same slot: otherwise a session accumulates a copy of everything browsed through.
        first, second = tmp_path / "a.mp3", tmp_path / "b.mp3"
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        stage = MediaSampleStage()
        stage.stage(first, slot="Clip")
        staged_first = Path(stage.directory) / "a.mp3"

        stage.stage(second, slot="Clip")

        assert not staged_first.exists()
        assert (Path(stage.directory) / "b.mp3").exists()

    def test_a_replace_that_fails_keeps_the_file_it_was_replacing(self, tmp_path):
        """A bad second pick must not cost the good sample the panel says it is testing with.

        Dropping the slot first lost both: the previous file was already unlinked when the copy
        raised, so ``directory`` went empty and the next Run declined with the tool's "no
        collection" reason while the row went on naming a file the stage no longer had.
        """
        good = tmp_path / "take.wav"
        good.write_bytes(b"good")
        stage = MediaSampleStage()
        stage.stage(good, slot="Clip")

        with pytest.raises(OSError):
            stage.stage(tmp_path / "never-existed.wav", slot="Clip")

        directory = Path(stage.directory)
        assert (directory / "take.wav").read_bytes() == b"good"
        # …and the failed attempt left nothing behind for `media_dir` to trip over.
        assert sorted(path.name for path in directory.iterdir()) == ["take.wav"]

    def test_replacing_a_slot_reuses_the_name_it_had(self, tmp_path):
        # The slot's own file is not "taken" — it is about to go. Counting it would suffix every
        # re-pick (take-1.wav, take-2.wav, …) and change the reference under the user.
        first, second = tmp_path / "one" / "take.wav", tmp_path / "two" / "take.wav"
        for path, payload in ((first, b"first"), (second, b"second")):
            path.parent.mkdir()
            path.write_bytes(payload)
        stage = MediaSampleStage()
        stage.stage(first, slot="Clip")

        name = stage.stage(second, slot="Clip")

        assert name == "take.wav"
        assert (Path(stage.directory) / "take.wav").read_bytes() == b"second"

    def test_two_inputs_stage_side_by_side_in_one_folder(self, tmp_path):
        """A tool declaring two media inputs needs both files at once.

        One folder because ``ToolContext.media_dir`` is ONE folder: the tool resolves both
        references against it, exactly as it would against the collection's media directory.
        """
        clip, picture = tmp_path / "a.mp3", tmp_path / "b.png"
        clip.write_bytes(b"1")
        picture.write_bytes(b"2")
        stage = MediaSampleStage()

        stage.stage(clip, slot="Clip")
        stage.stage(picture, slot="Picture")

        directory = Path(stage.directory)
        assert (directory / "a.mp3").read_bytes() == b"1"
        assert (directory / "b.png").read_bytes() == b"2"

    def test_a_name_collision_between_inputs_keeps_both_files(self, tmp_path):
        """Two inputs whose files happen to share a basename must not overwrite each other.

        Without the rename one reference silently resolves to the other input's bytes — a wrong
        test result that looks entirely right.
        """
        first = tmp_path / "one" / "take.mp3"
        second = tmp_path / "two" / "take.mp3"
        for path, payload in ((first, b"first"), (second, b"second")):
            path.parent.mkdir()
            path.write_bytes(payload)
        stage = MediaSampleStage()

        first_name = stage.stage(first, slot="Intro")
        second_name = stage.stage(second, slot="Outro")

        assert first_name != second_name
        directory = Path(stage.directory)
        assert (directory / first_name).read_bytes() == b"first"
        assert (directory / second_name).read_bytes() == b"second"

    def test_clearing_nothing_is_safe(self):
        stage = MediaSampleStage()

        stage.clear()  # must not raise on a stage that has never been used

        assert stage.directory == ""

    def test_clearing_removes_every_slot(self, tmp_path):
        clip, picture = tmp_path / "a.mp3", tmp_path / "b.png"
        clip.write_bytes(b"1")
        picture.write_bytes(b"2")
        stage = MediaSampleStage()
        stage.stage(clip, slot="Clip")
        stage.stage(picture, slot="Picture")
        directory = Path(stage.directory)

        stage.clear()

        assert list(directory.iterdir()) == []
        assert stage.directory == ""

    def test_dispose_removes_a_folder_it_created(self, tmp_path):
        source = tmp_path / "c.wav"
        source.write_bytes(b"x")
        stage = MediaSampleStage()
        stage.stage(source, slot="Clip")
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
        stage.stage(source, slot="Clip")

        stage.dispose()

        assert borrowed.exists()
        assert keeper.read_text() == "keep me"

    def test_a_borrowed_root_still_reports_empty_until_staged(self, tmp_path):
        # The folder exists from construction; the media reference a tool is about to resolve
        # is certainly not in it yet, so reporting the folder would be a lie a tool acts on.
        borrowed = tmp_path / "borrowed"
        borrowed.mkdir()

        assert MediaSampleStage(root=borrowed).directory == ""
