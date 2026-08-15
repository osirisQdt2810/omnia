"""The files a Try-it run pretends are in the collection, and how they are classified.

A user tool that reads media resolves a field's reference against ``ctx.media_dir()``. To test
such a tool against a file the user picks — which may live anywhere — that reference has to
resolve somewhere. Three options, and the third is the one taken:

* **Copy it into the collection.** It works, and it makes a test permanently change the
  collection: Anki syncs media, so testing a tool would push the sample to every device, and
  removing it afterwards pushes a deletion. A test must not touch the user's data.
* **Symlink it into the collection.** Same sync problem (Anki's media scanner follows it), plus
  symlink creation on Windows needs Developer Mode or elevation, so it fails on one of the
  three platforms this add-on ships to.
* **Stage it in a temp folder and point the TEST's ``media_dir`` there.** The reference
  resolves, the tool reads a real file, the collection is never touched, and nothing syncs.

So a Try-it media sample is copied into a per-session staging folder, and the tool under test
gets a context whose ``media_dir()`` is that folder. The collection is left exactly alone.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from omnia.plugins.smart_notes.engine.tools.base import INPUT_KIND_EXTENSIONS

#: Extensions Anki renders with <img>. Everything else it references with [sound:…], which is
#: also how it plays video — so the two-way split matches Anki's own behaviour rather than
#: enumerating formats this feature happens to have been asked about first.
#:
#: DERIVED from :data:`INPUT_KIND_EXTENSIONS`, never restated: this list and the picker's filter
#: are the same question asked twice, and when they were written out separately they drifted —
#: the picker hid the ``.bmp``/``.tiff`` scans this module was already calling pictures.
_IMAGE_SUFFIXES = frozenset(
    f".{extension}" for extension in INPUT_KIND_EXTENSIONS["image"]
)

#: The picture MIME types that are NOT ``image/<extension>``. Only the exceptions, so this is a
#: table of one fact each rather than a second copy of the extension vocabulary above: a format
#: added to :data:`INPUT_KIND_EXTENSIONS` needs an entry here only when its MIME differs from
#: its extension.
#:
#: Written out rather than taken from ``mimetypes``, which answers ``audio/x-flac`` and
#: ``audio/mp4a-latm`` for formats Chromium then refuses on the CONTENT TYPE, before it ever
#: tries to decode — making a perfectly good file look broken.
_IMAGE_MIME_EXCEPTIONS: dict[str, str] = {
    "jpg": "image/jpeg",  # `image/jpg` is not a real MIME type
    "svg": "image/svg+xml",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


def _extension(name_or_ext: str) -> str:
    """Return the bare lowercase extension of ``name_or_ext`` (which may already be one)."""
    tail = name_or_ext.rsplit(".", 1)[-1] if "." in name_or_ext else name_or_ext
    return tail.strip().lower()


def media_reference(name: str) -> str:
    """Return how a note field refers to the media file ``name``.

    Anki has exactly two forms — an ``<img>`` tag for pictures and ``[sound:…]`` for everything
    it plays, video included — so this branches on that rather than on a list of the formats
    this feature was first asked about. An unknown extension gets ``[sound:…]``, which is what
    Anki itself falls back to.

    Args:
        name: The bare file name.

    Returns:
        The reference text to put in the sample box.
    """
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in _IMAGE_SUFFIXES:
        return f'<img src="{name}">'
    return f"[sound:{name}]"


def media_family(name_or_ext: str) -> str:
    """Return which player a produced file needs: ``"image"``, ``"video"`` or ``"audio"``.

    This module already owns "classify a media file by its extension" (:func:`media_reference`
    does it for Anki's two reference forms), so the finer three-way split the Try-it output box
    needs lives beside it rather than in the dialog.

    Both lists are :data:`INPUT_KIND_EXTENSIONS`' own — reused, not restated, because a second
    copy is a second thing to keep in step. That constant is named for INPUT kinds and is being
    read to classify an OUTPUT; the tension is only in the name, since a container is a
    container whichever direction it travels.

    Args:
        name_or_ext: A file name, or a bare extension (with or without a leading dot).

    Returns:
        ``"image"`` for a picture, ``"video"`` for a container Anki plays with video, and
        ``"audio"`` for everything else — the same "Anki plays it" fallback
        :func:`media_reference` takes, so an unknown extension is handed to the player rather
        than declared unrenderable.
    """
    extension = _extension(name_or_ext)
    if f".{extension}" in _IMAGE_SUFFIXES:
        return "image"
    if extension in INPUT_KIND_EXTENSIONS["video"]:
        return "video"
    return "audio"


def image_data_uri(data: bytes, ext: str) -> str:
    """Return ``data`` as a ``data:`` URI the page can put in an ``<img>``.

    Args:
        data: The picture's bytes.
        ext: The extension the tool produced it under.

    Returns:
        ``data:<mime>;base64,<payload>`` — ``image/<ext>``, except for the formats
        :data:`_IMAGE_MIME_EXCEPTIONS` names, whose MIME is not their extension.
    """
    extension = _extension(ext)
    mime = _IMAGE_MIME_EXCEPTIONS.get(extension, f"image/{extension or 'png'}")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


class MediaSampleStage:
    """Holds the media files a Try-it run reads, outside the collection — one per input slot.

    The Try-it panel renders one control per input the tool declares, so a tool reading a clip
    AND a picture has two of them staged at once. They share ONE root because
    ``ToolContext.media_dir`` is a single folder: the tool resolves both references against it,
    exactly as it would against the collection's media directory.

    A slot holds one file at a time — picking again for the same input replaces it, which is
    also what deletes the previous file, so a session cannot accumulate copies of whatever the
    user browsed through.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        """Initialise the stage.

        Args:
            root: Where staged files live. Defaults to a fresh temp directory, created lazily
                so a session that never picks a file creates nothing. A root passed in is
                treated as BORROWED: it is used as-is and never deleted, because deleting a
                directory this object did not create is not its call to make.
        """
        self._root = root
        self._owns_root = root is None
        self._current: dict[str, Path] = {}

    @property
    def directory(self) -> str:
        """The folder a test's ``media_dir()`` should report, or "" when nothing is staged.

        Keyed on the staged FILES rather than on the folder existing: an injected root exists
        from construction, and reporting it before anything is in it would tell a tool "here is
        the media folder" when the reference it is about to resolve is certainly not there.
        """
        return str(self._root) if self._current else ""

    def stage(self, source: Path, *, slot: str) -> str:
        """Copy ``source`` in for the input ``slot``, replacing that slot's file, and name it.

        Args:
            source: The file the user picked.
            slot: The input the file was picked for (the note field's name). Only this slot's
                previous file is removed — another input's stays staged, or the tool would
                decline the moment the user picked its second file.

        Returns:
            The staged file's bare name — what a note would store in a media reference. It is
            the name the caller must build the reference from: two inputs picking files that
            happen to share a basename are kept apart by renaming the second, and a reference
            built from the ORIGINAL name would then resolve to the other input's bytes — a
            wrong test result that looks right.

        Raises:
            OSError: If the file cannot be read or copied — and the slot then still holds the
                file it held before, because the replace is done copy-first.
        """
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="omnia-sample-"))
        previous = self._current.get(slot)
        target = self._root / self._free_name(source.name, slot=slot)
        # Copy into a scratch file and swap it in, rather than clearing the slot first: a
        # replace that fails (the new file was moved, or cannot be read) must leave the good
        # file the panel still says it is testing against — otherwise a bad second pick both
        # loses the sample AND empties `directory`, and the next Run declines with "no
        # collection" while the row goes on naming a file the stage no longer has.
        handle, scratch_name = tempfile.mkstemp(dir=self._root, prefix=".omnia-part-")
        os.close(handle)
        scratch = Path(scratch_name)
        try:
            shutil.copy2(source, scratch)
            os.replace(scratch, target)
        except OSError:
            scratch.unlink(missing_ok=True)
            raise
        if previous is not None and previous != target:
            # missing_ok: the folder is a temp dir anything may have cleaned up, and failing to
            # delete a file that is already gone is not worth taking a dialog down for.
            previous.unlink(missing_ok=True)
        self._current[slot] = target
        return target.name

    def _free_name(self, name: str, *, slot: str) -> str:
        """Return ``name``, or a suffixed variant when ANOTHER slot already holds that name.

        This slot's own file does not count as taken: it is about to be replaced, and suffixing
        around it would rename the sample on every re-pick (``take.wav`` → ``take-1.wav`` → …).
        """
        taken = {path.name for held, path in self._current.items() if held != slot}
        if name not in taken:
            return name
        stem, suffix = Path(name).stem, Path(name).suffix
        index = 1
        while f"{stem}-{index}{suffix}" in taken:
            index += 1
        return f"{stem}-{index}{suffix}"

    def clear(self) -> None:
        """Remove every staged file. Safe to call when nothing is staged."""
        for path in self._current.values():
            # missing_ok: see stage() — a temp file that is already gone is not a failure.
            path.unlink(missing_ok=True)
        self._current.clear()

    def dispose(self) -> None:
        """Drop everything this object created — called when the dialog closes.

        Only removes the FOLDER when this object made it. A borrowed root belongs to whoever
        passed it in, and quietly rmtree-ing someone else's directory is the kind of thing this
        class exists to avoid doing to a collection.
        """
        self.clear()
        if self._root is not None and self._owns_root:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None
