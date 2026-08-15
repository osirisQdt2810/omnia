"""On-demand piper voice models — resolve locally, else fetch once into ``user_files``.

A piper voice is a PAIR of files that must live in the same directory: ``<voice>.onnx`` (the
weights, tens of MB) and ``<voice>.onnx.json`` (the inference config piper loads by guessing
``<model>.json``). Shipping the weights inside the ``.ankiaddon`` made the package ~57 MB, of
which the weights were ~99% — downloaded by every user on install AND again on every update,
for a Vietnamese voice most of them will never play. So the package now carries only the small
config + README, and the weights are fetched the first time someone actually synthesizes.

:class:`PiperVoiceStore` owns that decision. Resolution order is the ``user_files`` copy (the
one directory Anki preserves across add-on updates), then the copy bundled/checked out beside
the add-on (developers have it through Git LFS), then a one-time download. A download writes
into a temp file inside the destination directory, refuses to write past the byte count the
catalog pins, verifies that count and the SHA-256, ``fsync``s, and only then ``os.replace``s it
into place — so a killed, truncated, over-long or corrupted transfer can never be mistaken for
a complete model. That is the same property the install marker gives a half-finished venv in
:mod:`omnia.core.runtime.native_runtime`, reached the way a single file can reach it:
atomically.

Every failure — a dead network, a chunked body that stops mid-stream, a full disk, a wrong
digest, the user clicking Cancel — leaves nothing behind and surfaces as ONE
:class:`~omnia.core.providers.errors.ProviderError` naming the voice. A user staring at a
generation that failed must never be handed a stdlib traceback instead.

Pure and headless-importable — no ``aqt``/``anki`` at module top (``addon_user_files_dir`` is
lazy-imported inside :func:`default_voice_store`), and every byte of network I/O goes through
the injectable :class:`ByteStreamer` seam, so the whole ladder unit-tests with no network.

Why this lives under ``tts/`` rather than at the ``providers/`` root: piper is the only
provider that carries model weights, the catalog entries ARE voices (the URL is derived from
the voice id), and there is exactly one caller. A kind-agnostic "large asset downloader" would
be an abstraction with no second user — the thing CONVENTIONS Part 3 tells us not to build.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from omnia.core.logging import get_logger
from omnia.core.providers.errors import ProviderError

_logger = get_logger("piper_voices")

# Streaming read size. 256 KiB gives a 60 MB voice ~250 progress ticks — enough for a moving
# percentage — without a syscall every few kilobytes.
_CHUNK_BYTES = 256 * 1024

# Per-read socket timeout. Generous: the transfer is large and a user's link may be slow, but a
# genuinely dead connection still gives up rather than hanging the background op forever.
_DOWNLOAD_TIMEOUT = 60

# Upstream voice repository. ``python -m piper.download_voices`` builds exactly this URL shape,
# so a voice id decomposes into the path: ``vi_VN-vais1000-medium`` → ``vi/vi_VN/vais1000/medium``.
_VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


@dataclass(frozen=True)
class RemoteAsset:
    """One downloadable file, with the exact bytes the catalog expects it to have."""

    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PiperVoiceModel:
    """A downloadable piper voice: its weights plus the config that must sit beside them."""

    voice: str
    weights: RemoteAsset
    config: RemoteAsset

    @property
    def assets(self) -> tuple[RemoteAsset, ...]:
        """Every file that must land on disk for the voice to be usable."""
        return (self.weights, self.config)

    @property
    def size_label(self) -> str:
        """Human-readable total download size, e.g. ``"63.2 MB"`` (for the progress line)."""
        return f"{(self.weights.size + self.config.size) / 1_000_000:.1f} MB"


def _catalog_entry(
    voice: str,
    *,
    weights_sha256: str,
    weights_size: int,
    config_sha256: str,
    config_size: int,
) -> PiperVoiceModel:
    """Build a catalog entry, deriving both URLs from the voice id.

    Args:
        voice: The piper voice id, ``<lang_code>-<name>-<quality>``.
        weights_sha256: Expected SHA-256 of the ``.onnx``.
        weights_size: Expected byte count of the ``.onnx``.
        config_sha256: Expected SHA-256 of the ``.onnx.json``.
        config_size: Expected byte count of the ``.onnx.json``.

    Returns:
        The assembled :class:`PiperVoiceModel`.
    """
    lang_code, name, quality = voice.split("-")
    family = lang_code.split("_")[0]
    base = f"{_VOICES_BASE_URL}/{family}/{lang_code}/{name}/{quality}/{voice}"
    return PiperVoiceModel(
        voice=voice,
        weights=RemoteAsset(
            filename=f"{voice}.onnx",
            url=f"{base}.onnx?download=true",
            size=weights_size,
            sha256=weights_sha256,
        ),
        config=RemoteAsset(
            filename=f"{voice}.onnx.json",
            url=f"{base}.onnx.json?download=true",
            size=config_size,
            sha256=config_sha256,
        ),
    )


# Voices the add-on can fetch on demand. The weights digest is the repo's OWN Git-LFS oid for
# the file that used to ship, which is byte-identical to what HuggingFace reports as the file's
# ``x-linked-etag`` — so the pin is verifiable from this checkout rather than a value taken on
# trust from the network the download itself uses.
DOWNLOADABLE_VOICES: dict[str, PiperVoiceModel] = {
    model.voice: model
    for model in (
        _catalog_entry(
            "vi_VN-vais1000-medium",
            weights_sha256="ec7c89e2c85f4d1edc24b6120c18aaf1bda614f06b511567eb9c7c0de15e2dab",
            weights_size=63201294,
            config_sha256="fafb9da1354ed4b77c31af228ed41fb41cd825c14cffa105454b25e6ae751ee0",
            config_size=4860,
        ),
    )
}


class DownloadFeedback:
    """The progress dialog a download talks to — both directions of one conversation.

    Reporting a status line and asking whether the user gave up belong to the SAME dialog, so
    they travel as one object rather than as two parallel callbacks threaded through every
    method. This base is deliberately inert: a download nobody is watching (a test, a headless
    resolve, the silent review-time pre-generation path) reports nowhere and is never
    cancelled. :class:`~omnia.core.providers.tts.piper.AnkiProgressFeedback` is the real one.
    """

    def report(self, message: str) -> None:
        """Show one short human-readable status line. Best-effort: must never raise."""

    def cancelled(self) -> bool:
        """Whether the user asked to stop, polled between chunks of a multi-minute fetch."""
        return False


class _CancelledError(Exception):
    """Internal signal that :meth:`DownloadFeedback.cancelled` went true mid-transfer.

    Private and converted at the ``_fetch`` boundary: a user who clicked Cancel gets a message
    about cancelling, not the "check your internet connection" advice a real failure earns.
    """


@runtime_checkable
class ByteStreamer(Protocol):
    """Injected transport that yields a URL's body in chunks, so a fetch can report progress."""

    def stream(self, url: str) -> Iterator[bytes]:
        """Yield the response body of ``url`` in chunks.

        Raises:
            ProviderError: On any transport failure (no network, 404, timeout, a truncated
                chunked transfer).
        """
        ...


class UrllibByteStreamer:
    """Stdlib streaming GET — nothing to vendor, identical on macOS and Windows.

    Deliberately NOT :class:`~omnia.core.network.http.HttpClient`: that seam hands back whole
    bodies, so a 60 MB voice would be buffered in RAM and could report no progress at all. It is
    also deliberately retry-free — a failed fetch leaves nothing behind and is retried by the
    user's next synthesis, so a retry loop here would only make a dead network take three times
    as long to say so.
    """

    def __init__(self, timeout: int = _DOWNLOAD_TIMEOUT) -> None:
        self._timeout = timeout

    def stream(self, url: str) -> Iterator[bytes]:
        """Yield ``url``'s body in :data:`_CHUNK_BYTES` chunks; raise ProviderError on failure."""
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as response:
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        return
                    yield chunk
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"HTTP {exc.code} from {url}") from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            # http.client.HTTPException is NOT an OSError: a chunked transfer that dies
            # mid-body raises IncompleteRead, which would otherwise walk straight past every
            # handler here and reach the user as a raw traceback naming no voice.
            http.client.HTTPException,
        ) as exc:
            raise ProviderError(
                f"network error reaching {url}: {_reason(exc)}"
            ) from exc


class PiperVoiceStore:
    """Resolve a piper voice to a usable ``.onnx``, fetching it once into ``user_files``.

    Two directories are searched, in this order, before anything is downloaded:

    1. ``user_dir`` — where a previous download landed. Anki preserves ``user_files`` across
       add-on updates, so a voice is fetched once per machine, not once per release.
    2. ``bundled_dir`` — the ``models/piper`` folder beside the add-on. A developer checkout
       still has the real weights via Git LFS, and a user can drop their own voice there.

    Only then does :meth:`resolve` download, and only for a voice the catalog knows. Every
    collaborator that touches the world (the transport, both directories, the catalog) is
    injected, so the whole ladder is exercisable offline in ``tmp_path``.
    """

    def __init__(
        self,
        user_dir: Path,
        bundled_dir: Path,
        *,
        streamer: Optional[ByteStreamer] = None,
        catalog: Optional[Mapping[str, PiperVoiceModel]] = None,
    ) -> None:
        """Initialise the store.

        Args:
            user_dir: Where downloaded voices are written (under ``user_files``).
            bundled_dir: The voices directory shipped/checked out beside the add-on.
            streamer: Transport seam; defaults to a real :class:`UrllibByteStreamer`.
            catalog: Voice metadata; defaults to :data:`DOWNLOADABLE_VOICES`.
        """
        self._user_dir = user_dir
        self._bundled_dir = bundled_dir
        self._streamer: ByteStreamer = (
            streamer if streamer is not None else UrllibByteStreamer()
        )
        self._catalog: Mapping[str, PiperVoiceModel] = (
            catalog if catalog is not None else DOWNLOADABLE_VOICES
        )
        # One lock per voice, so two note fields synthesizing at once (separate worker threads)
        # can't both start the same 60 MB download. The guard protects the lazy lock creation.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- resolution -----------------------------------------------------------------------
    def resolve(self, voice: str, feedback: Optional[DownloadFeedback] = None) -> Path:
        """Return the on-disk ``.onnx`` for the voice id ``voice``, downloading it if needed.

        Args:
            voice: A voice id such as ``"vi_VN-vais1000-medium"`` (no path, no suffix).
            feedback: The dialog to report download progress into and to poll for Cancel;
                omitted (or inert) when nobody is watching.

        Returns:
            Path to a complete ``.onnx`` whose ``.onnx.json`` sits next to it.

        Raises:
            ProviderError: If the voice is neither present locally nor downloadable, or the
                download fails / is cancelled / arrives corrupted.
        """
        model = self._catalog.get(voice)
        for directory in (self._user_dir, self._bundled_dir):
            if self._holds_voice(directory, voice, model):
                return directory / f"{voice}.onnx"
        if model is None:
            raise ProviderError(
                f"piper voice {voice!r} is not installed and is not one of the voices Omnia "
                f"can download ({', '.join(sorted(self._catalog)) or 'none'}). Pick a listed "
                f"voice, or put {voice}.onnx (+ {voice}.onnx.json) in {self._user_dir}."
            )
        return self._download(
            model, feedback if feedback is not None else DownloadFeedback()
        )

    def knows(self, voice: str) -> bool:
        """Whether ``voice`` is a catalog id this store can download.

        Exists so callers can tell a VOICE ID from a FILE NAME without importing the catalog
        and without hard-coding it — a store built with a different catalog (tests do) must
        answer for its own.
        """
        return voice in self._catalog

    def local_path(self, filename: str | Path) -> Path:
        """Return the first EXISTING copy of ``filename`` (user dir, then bundled dir).

        Falls back to the BUNDLED path when neither exists — nothing is downloaded here. This
        serves a voice the user named as a file rather than as a catalog id, and the fallback
        keeps the eventual "not found at <path>" error pointing at the directory they are meant
        to drop the file into.
        """
        for directory in (self._user_dir, self._bundled_dir):
            candidate = directory / filename
            if candidate.is_file():
                return candidate
        return self._bundled_dir / filename

    def _holds_voice(
        self, directory: Path, voice: str, model: Optional[PiperVoiceModel]
    ) -> bool:
        """Whether ``directory`` holds a USABLE copy of ``voice``.

        For a catalog voice the weights must be exactly the expected byte count. That single
        check is what stops a truncated download — or a Git-LFS *pointer* in a checkout where
        ``git lfs pull`` never ran (a ~130-byte text file that is still very much a file) — from
        being handed to piper as a model. It is a ``stat``, not a re-hash: the SHA-256 is
        verified once, when the bytes arrive, and re-hashing 60 MB on every single synthesis
        would cost far more than it could catch.

        Only the WEIGHTS are size-checked. The sibling ``.onnx.json`` is a text blob that git may
        rewrite the line endings of on a Windows checkout, so requiring an exact size there would
        reject a perfectly good developer copy; its presence is what piper actually needs. An
        off-catalog voice the user supplied has no expected size at all, so existence is all we
        can ask of it.
        """
        weights = directory / f"{voice}.onnx"
        if not weights.is_file():
            return False
        if model is None:
            return True
        if not (directory / model.config.filename).is_file():
            return False
        return weights.stat().st_size == model.weights.size

    # -- download -------------------------------------------------------------------------
    def _download(self, model: PiperVoiceModel, feedback: DownloadFeedback) -> Path:
        """Fetch every asset of ``model`` into the user dir and return the weights path."""
        with self._lock_for(model.voice):
            # Re-check under the lock: while we waited, the thread that held it may have
            # finished the very download we were about to start.
            if self._holds_voice(self._user_dir, model.voice, model):
                return self._user_dir / model.weights.filename
            try:
                self._user_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Same reason the temp-file creation is guarded: an unwritable add-on directory
                # must name the voice, not raise a bare PermissionError about a path the user
                # has never heard of.
                raise ProviderError(
                    self._failure(model.voice, model.weights, _reason(exc))
                ) from exc
            self._sweep_stale_parts(model)
            _logger.info(
                "downloading piper voice %s (%s) into %s",
                model.voice,
                model.size_label,
                self._user_dir,
            )
            feedback.report(
                f"Downloading the piper voice {model.voice} "
                f"({model.size_label}) — this happens once."
            )
            for asset in model.assets:
                self._fetch(asset, model.voice, feedback)
            _logger.info("piper voice %s is ready", model.voice)
            feedback.report(f"Piper voice {model.voice} ready.")
            return self._user_dir / model.weights.filename

    def _sweep_stale_parts(self, model: PiperVoiceModel) -> None:
        """Delete ``.part`` leftovers a previous run was killed before it could clean up.

        ``user_files`` is the one directory Anki carries across every add-on update, and
        :func:`tempfile.mkstemp` picks a fresh random name per attempt — so a machine that lost
        power at 80% three times keeps three ~50 MB orphans forever unless something sweeps.

        Safe here: it runs under the per-voice lock, so no fetch of THIS voice is in flight in
        this process. A file another process still holds open (Windows locks those) simply
        refuses to unlink; that is logged and skipped rather than failing the download.
        """
        for asset in model.assets:
            for stale in self._user_dir.glob(f".{asset.filename}.*.part"):
                try:
                    stale.unlink()
                except OSError:
                    _logger.debug(
                        "could not remove stale part %s", stale, exc_info=True
                    )

    def _fetch(
        self, asset: RemoteAsset, voice: str, feedback: DownloadFeedback
    ) -> None:
        """Download one asset: temp file → verify size + SHA-256 → atomic replace.

        The temp file is created INSIDE the destination directory so the final ``os.replace`` is
        a same-filesystem rename, which is atomic on both macOS and Windows: a concurrent reader
        sees either no file or the whole verified one, never a half-written model. Anything that
        goes wrong takes the temp file with it — including an exception class nobody anticipated
        and an interpreter-level ``BaseException``, which is why the cleanup is a ``finally``.

        Raises:
            ProviderError: On ANY failure — transport, truncation, over-long body, local write,
                size/digest mismatch, or the user cancelling — always naming the voice.
        """
        dest = self._user_dir / asset.filename
        # stat() and mkstemp() are inside the funnel too: on a read-only or antivirus-locked
        # add-on directory they raise PermissionError, and escaping here would surface a random
        # .part filename the user has never seen instead of the voice and the manual-install
        # advice. This is the FIRST-download path, so it is the likeliest place to meet a
        # directory we cannot write.
        try:
            if dest.is_file() and dest.stat().st_size == asset.size:
                return  # already landed (a previous attempt that failed on a later asset)
            handle, tmp_name = tempfile.mkstemp(
                dir=self._user_dir, prefix=f".{asset.filename}.", suffix=".part"
            )
        except OSError as exc:
            raise ProviderError(self._failure(voice, asset, _reason(exc))) from exc
        tmp = Path(tmp_name)
        landed = False
        try:
            digest, written = self._stream_into(handle, asset, feedback)
            mismatch = _mismatch(asset, written, digest)
            if mismatch:
                raise ProviderError(mismatch)  # composed by the handler below
            os.replace(tmp, dest)
            landed = True
        except _CancelledError:
            raise ProviderError(
                f"Downloading the piper voice {voice!r} was cancelled — nothing partial "
                f"was kept."
            ) from None
        except Exception as exc:
            # ONE funnel for every failure class, because the ones that hurt are the ones we
            # did not think of: a transport ProviderError, an http.client.IncompleteRead
            # (neither OSError nor ProviderError), a local-write OSError, a rejected digest.
            # Whatever arrives, the user reads one message that names the voice — and the log
            # keeps the real traceback, so a BUG that lands here is still diagnosable rather
            # than permanently disguised as "check your internet connection".
            _logger.warning(
                "piper voice %s: fetching %s failed",
                voice,
                asset.filename,
                exc_info=True,
            )
            raise ProviderError(self._failure(voice, asset, _reason(exc))) from exc
        finally:
            # A `finally`, not another except arm: a KeyboardInterrupt or a SystemExit mid
            # transfer must not leave tens of MB of ".part" behind in user_files either.
            if not landed:
                tmp.unlink(missing_ok=True)

    def _stream_into(
        self, handle: int, asset: RemoteAsset, feedback: DownloadFeedback
    ) -> tuple[str, int]:
        """Write ``asset``'s body to the open descriptor, hashing and reporting as it goes.

        Args:
            handle: Open file descriptor for the temp file (taken over and closed here).
            asset: The file being fetched, with the exact size the catalog pins.
            feedback: Progress sink, polled for Cancel between chunks.

        Returns:
            ``(hex_digest, bytes_written)`` of what was actually written.

        Raises:
            _CancelledError: If the user clicked Cancel.
            ProviderError: If the body runs past the pinned size, or the transport fails.
            OSError: If the local write fails (a full disk).
        """
        digest = hashlib.sha256()
        written = 0
        last_percent = -1
        with os.fdopen(handle, "wb") as out:
            for chunk in self._streamer.stream(asset.url):
                if feedback.cancelled():
                    raise _CancelledError()
                if written + len(chunk) > asset.size:
                    # Reject the FIRST over-long chunk rather than discovering the mismatch
                    # after the body is on disk. On the one path whose whole job is to bound a
                    # large write, a broken proxy or a hostile server must not be able to fill
                    # the user's disk with bytes we already know we are going to throw away.
                    raise ProviderError(
                        f"the download is larger than the expected "
                        f"{asset.size / 1_000_000:.1f} MB"
                    )
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                percent = min(100, written * 100 // asset.size) if asset.size else 100
                if percent != last_percent:
                    last_percent = percent
                    feedback.report(_progress_line(asset, written, percent))
            # fsync before the caller's os.replace: the rename is journaled, the DATA is not.
            # A host crash could otherwise leave a file at the FINAL name whose blocks were
            # never written — right-sized but zero-filled, which the size-only check in
            # _holds_voice would then accept on every future resolve, forever.
            out.flush()
            os.fsync(out.fileno())
        return digest.hexdigest(), written

    def _failure(self, voice: str, asset: RemoteAsset, reason: str) -> str:
        """Compose the one message a failed voice download is allowed to show the user.

        It names the voice (there may be several, and a bare URL says nothing about which one
        failed), states the reason, promises nothing partial was kept, and gives the manual way
        out — which is the only actionable advice for a user behind a blocking proxy.
        """
        return (
            f"Could not download the piper voice {voice!r}: {reason}. Nothing partial was "
            f"kept — check your internet connection and try again. To install it by hand, "
            f"download {asset.url.split('?')[0]} and put it in {self._user_dir}."
        )

    def _lock_for(self, voice: str) -> threading.Lock:
        """Return the (lazily created) download lock for ``voice`` — one lock per voice."""
        with self._locks_guard:
            lock = self._locks.get(voice)
            if lock is None:
                lock = threading.Lock()
                self._locks[voice] = lock
            return lock


def _reason(exc: BaseException) -> str:
    """Describe ``exc`` for the user-facing failure line.

    Falls back to the class name because several stdlib transport errors stringify to ``""``
    (a bare ``http.client.IncompleteRead`` subclass, some ``socket`` errors), and "Could not
    download the piper voice 'x': ." tells nobody anything.
    """
    return str(exc) or type(exc).__name__


def _progress_line(asset: RemoteAsset, written: int, percent: int) -> str:
    """Render one download progress line (``"name: 7.6/63.2 MB (12%)"``)."""
    return (
        f"{asset.filename}: {written / 1_000_000:.1f}/"
        f"{asset.size / 1_000_000:.1f} MB ({percent}%)"
    )


def _mismatch(asset: RemoteAsset, written: int, digest: str) -> str:
    """Return why the downloaded bytes are not ``asset``, or ``""`` when they are.

    Size is checked first because it is the failure that actually happens — a connection cut
    mid-transfer — and its message ("got 41.0 of 63.2 MB") tells the user what went wrong. The
    digest then catches the rarer silent corruption a correct byte count would hide.
    """
    if written != asset.size:
        return (
            f"the download stopped early (got {written / 1_000_000:.1f} of "
            f"{asset.size / 1_000_000:.1f} MB)"
        )
    if digest != asset.sha256:
        return "the downloaded file is corrupted (checksum mismatch)"
    return ""


def bundled_models_dir() -> Path:
    """Locate the ``models/piper`` dir shipped / checked out beside the add-on.

    ``models`` is NOT inside the source package — it sits at the repo/add-on root. Two layouts
    resolve differently from ``__file__`` (``<root>/core/providers/tts/voice_models.py``):

    * Deployed (per-item symlinks): ``models`` is a symlinked SIBLING of the package items, so
      it is ``parents[3]/models`` — using the un-resolved path so the ``core`` symlink isn't
      chased back into ``src/omnia`` (where no ``models`` sibling exists).
    * Dev/headless (``src/omnia`` package, repo-root ``models``): it is ``parents[5]/models``.

    Prefer whichever ``models/piper`` actually exists; fall back to the deploy location so a
    fresh install still names a sensible directory in its messages.
    """
    here = Path(__file__)
    candidates = (
        here.parents[3] / "models" / "piper",  # deployed add-on root sibling
        here.parents[5] / "models" / "piper",  # dev repo root
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve(strict=False)
    return candidates[0].resolve(strict=False)


# Lazily-built process-wide store. Not constructed at import (it would resolve Anki paths);
# callers go through ``default_voice_store``.
_default_store: Optional[PiperVoiceStore] = None
_default_store_guard = threading.Lock()


def default_voice_store() -> PiperVoiceStore:
    """Return the process-wide store: fetched voices in ``<user_files>/models/piper``.

    ``user_files`` is the one directory Anki preserves across add-on updates, so a voice
    downloaded today survives every future release. ``addon_user_files_dir`` is imported here
    (not at module top) so this module stays ``aqt``-free and headless-importable — the same
    shape as :func:`omnia.core.runtime.native_runtime.default_manager`.

    Built under a lock because ``mw.taskman`` runs several workers: an editor generation and a
    Studio preview that first-touch this at the same moment must end up with ONE store, or each
    gets its own per-voice lock dict and the same 60 MB is fetched twice.
    """
    global _default_store
    with _default_store_guard:
        if _default_store is None:
            from omnia import addon_user_files_dir

            _default_store = PiperVoiceStore(
                user_dir=addon_user_files_dir() / "models" / "piper",
                bundled_dir=bundled_models_dir(),
            )
        return _default_store
