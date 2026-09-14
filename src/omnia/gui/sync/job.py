"""One pull, from the moment the button is pressed until it is over.

The user can put the window away and carry on studying while this runs — that is the whole reason
it is an object rather than a function. So it has to answer, at any moment and from any thread,
"what are you doing and how much longer", and it has to be findable without the window that
started it.

What it protects, in the order the risk shows up:

* **One pull at a time.** Two imports into one collection at once is a way to lose data that no
  backup makes obvious afterwards. A second request is refused with a sentence, not queued.
* **A backup before anything is written.** Everything up to the import only reads; the import is
  the one irreversible step, and Anki's own backup folder is where the file goes so the user can
  find it in Anki's own restore list.
* **A partial download is never imported.** The transfer checks the size it was promised and
  deletes what it wrote; this never sees a half package.
* **The temp file goes away.** On success, on failure, and when the profile closes.

Threading: the network runs on a worker thread through :func:`anki_compat.run_in_background`, and
the import runs on the Qt main thread because it writes to the collection. Progress is read by
whoever is drawing, whenever they draw — no callbacks into the UI, so a closed window cannot be
called back into.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.sync.package import PackageOffer, PackageRequest
from omnia.core.sync.progress import (
    DONE,
    DOWNLOADING,
    FAILED,
    IMPORTING,
    PREPARING,
    Progress,
    ProgressTracker,
)

logger = get_logger("sync")


class PullJob:
    """A pull in flight.

    Args:
        client: Already pointed at the other machine.
        request: What to bring.
        repo: The config repository, for the settings that travel beside the package.
        machine: What to call the other machine in a sentence.
    """

    def __init__(
        self,
        client: Any,
        request: PackageRequest,
        repo: Any,
        machine: str = "",
        policy: str = "",
    ) -> None:
        self._client = client
        self._request = request
        self._repo = repo
        self._policy = policy
        self._machine = machine or "the other computer"
        self._tracker = ProgressTracker(time.monotonic)
        self._lock = threading.Lock()
        self._path: Optional[str] = None
        self._finished = False
        self._result: str = ""

    # --- what anybody drawing this needs --------------------------------------------------
    def snapshot(self) -> Progress:
        """Where it has got to. Safe from any thread, and cheap enough to poll."""
        with self._lock:
            return self._tracker.snapshot()

    @property
    def running(self) -> bool:
        """Whether this pull is still going."""
        return not self._finished

    @property
    def result(self) -> str:
        """The closing sentence, once there is one."""
        return self._result

    # --- the run ---------------------------------------------------------------------------
    def start(self, on_change: Optional[Callable[[], None]] = None) -> None:
        """Begin. Returns at once; the work happens elsewhere."""
        self._on_change = on_change or (lambda: None)
        anki_compat.run_in_background(
            self._fetch,
            on_success=self._import,
            on_failure=self._failed,
            label=f"Omnia: copying from {self._machine}…",
        )

    def _fetch(self) -> str:
        """Ask for the package and bring it over. Runs on a worker thread."""
        self._phase(PREPARING)
        offer: PackageOffer = self._client.request_package(self._request)
        self._phase(DOWNLOADING)
        with self._lock:
            self._tracker.set_total(offer.bytes)
        path = _destination()
        self._path = path
        self._client.download_package(offer, path, on_progress=self._advance)
        return path

    def _advance(self, done: int) -> None:
        with self._lock:
            self._tracker.advance(done)
        self._on_change()

    def _phase(self, phase: str, *, detail: str = "") -> None:
        with self._lock:
            self._tracker.set_phase(phase, detail=detail)
        self._on_change()

    def _import(self, path: str) -> None:
        """Add it to this collection. Runs on the Qt main thread, because it writes."""
        from omnia.gui.sync.apply import apply_package, backup_first

        self._phase(IMPORTING)
        try:
            backup_first(f"before copying from {self._machine}")
            result = apply_package(path, self._policy)
        except Exception as exc:
            logger.exception("sync: could not add the package to this collection")
            self._fail(
                "The copy arrived but could not be added to this collection. Nothing was "
                f"changed — see the Omnia log. ({exc})"
            )
            return
        finally:
            self._discard()
        self._finish(result.summary)

    def _failed(self, exc: BaseException) -> None:
        from omnia.core.sync import SyncError

        if not isinstance(exc, SyncError):
            logger.exception("sync: the pull failed", exc_info=exc)
        self._discard()
        self._fail(
            str(exc)
            if isinstance(exc, SyncError)
            else "Something went wrong on this computer — see the Omnia log."
        )

    def _fail(self, message: str) -> None:
        self._result = message
        self._phase(FAILED, detail=message)
        self._finished = True
        _release(self)

    def _finish(self, summary: str) -> None:
        self._result = summary
        self._phase(DONE, detail=summary)
        self._finished = True
        _release(self)
        logger.info("sync: pull finished — %s", summary)

    def _discard(self) -> None:
        path, self._path = self._path, None
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            logger.warning("sync: could not remove the downloaded package at %s", path)


#: The one pull allowed at a time. Module-level because it outlives every window that can show it
#: — that is the feature, not an accident: the user closes the picker and keeps studying.
_CURRENT: Optional[PullJob] = None
_CURRENT_LOCK = threading.Lock()


class PullRefusedError(RuntimeError):
    """A pull that must not start, with a reason a person can act on."""


def start_pull(
    client: Any,
    request: PackageRequest,
    repo: Any,
    *,
    machine: str = "",
    policy: str = "",
    on_change: Optional[Callable[[], None]] = None,
) -> PullJob:
    """Begin a pull, if none is already running.

    Raises:
        PullRefusedError: When one is. Refused rather than queued: two imports into one collection is
            a way to lose data that no backup makes obvious afterwards, and a user who pressed the
            button twice wants to know the first one is still going.
    """
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is not None and _CURRENT.running:
            raise PullRefusedError(
                "A copy is already running. Wait for it to finish before starting another."
            )
        job = PullJob(client, request, repo, machine=machine, policy=policy)
        _CURRENT = job
    job.start(on_change)
    return job


def current() -> Optional[PullJob]:
    """The pull in flight, or None. What the Sync button draws itself from."""
    with _CURRENT_LOCK:
        return _CURRENT


def _release(job: PullJob) -> None:
    """Mark a job finished. It is KEPT, not dropped.

    Because the whole point is that the user walked away: they closed the picker, carried on
    studying, and the Sync button is where they find out how it went. A job dropped the instant
    it succeeded means the one moment they were not looking is the one moment it could be seen.

    It is replaced when the next pull starts, and cleared when the profile closes.
    """
    _ = job


def forget() -> None:
    """Drop whatever is remembered. Called when the profile closes."""
    global _CURRENT
    with _CURRENT_LOCK:
        job, _CURRENT = _CURRENT, None
    if job is not None:
        job._discard()


def _destination() -> str:
    """Where an arriving package is written."""
    import tempfile

    handle, path = tempfile.mkstemp(prefix="omnia-pull-", suffix=".apkg")
    os.close(handle)
    return path
