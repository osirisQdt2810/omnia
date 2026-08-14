"""The file a Try-it run pretends is in the collection.

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

import shutil
import tempfile
from pathlib import Path
from typing import Optional

#: Extensions Anki renders with <img>. Everything else it references with [sound:…], which is
#: also how it plays video — so the two-way split matches Anki's own behaviour rather than
#: enumerating formats this feature happens to have been asked about first.
_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".bmp", ".tif", ".tiff"}
)


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


class MediaSampleStage:
    """Holds the ONE media file a Try-it run reads, outside the collection.

    One file at a time on purpose: the panel has one sample box, and keeping a history would
    mean deciding when a previous pick stops being interesting. Picking again replaces it —
    which is also what deletes the previous file, so a session cannot accumulate copies of
    whatever the user browsed through.
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
        self._current: Optional[Path] = None

    @property
    def directory(self) -> str:
        """The folder a test's ``media_dir()`` should report, or "" when nothing is staged.

        Keyed on the staged FILE rather than on the folder existing: an injected root exists
        from construction, and reporting it before anything is in it would tell a tool "here is
        the media folder" when the reference it is about to resolve is certainly not there.
        """
        return str(self._root) if self._current is not None else ""

    def stage(self, source: Path) -> str:
        """Copy ``source`` into the stage, replacing whatever was there, and return its name.

        Args:
            source: The file the user picked.

        Returns:
            The staged file's bare name — what a note would store in a media reference.

        Raises:
            OSError: If the file cannot be read or copied.
        """
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="omnia-sample-"))
        self.clear()
        target = self._root / source.name
        shutil.copy2(source, target)
        self._current = target
        return target.name

    def clear(self) -> None:
        """Remove the staged file, if any. Safe to call when nothing is staged."""
        if self._current is not None:
            # missing_ok: the folder is a temp dir anything may have cleaned up, and failing to
            # delete a file that is already gone is not worth taking a dialog down for.
            self._current.unlink(missing_ok=True)
            self._current = None

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
