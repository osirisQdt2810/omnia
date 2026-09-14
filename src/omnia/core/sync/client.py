"""The target machine's side of a sync: asking another machine what it has.

The whole of this module is one idea — **every failure gets a sentence the user can act on**.
A pull that cannot start is the normal case while someone is setting this up: the other machine
is asleep, sharing is off over there, the ID was regenerated last week, the two builds differ.
Each of those needs a different action, and "could not connect" tells the user none of them.

So :class:`SyncClient` maps each failure to its own message, and the one thing it never does is
report a cause it has not established: a connection refused means *something answered and said
no*, a timeout means *nothing answered at all*, and those are different sentences.

Stdlib only, no ``aqt``, no ``anki`` — the caller is responsible for keeping it off the Qt main
thread (``QueryOp``/``mw.taskman``), because a machine that is switched off takes the full
timeout to say so.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import quote

from omnia.core.logging import get_logger
from omnia.core.sync.inventory import PROTOCOL, Inventory, InventoryError
from omnia.core.sync.package import PackageError, PackageOffer, PackageRequest
from omnia.core.sync.pairing import PairingAddress
from omnia.core.sync.service import (
    HELLO_PATH,
    INVENTORY_PATH,
    PACKAGE_PATH,
    TOKEN_HEADER,
)

#: How much of a package is read at a time. Small enough that hundreds of megabytes are never
#: held in memory, big enough that it is not a syscall per kilobyte.
_CHUNK_BYTES = 256 * 1024

#: How long to wait on the other machine. Long enough for a collection read behind a busy Qt main
#: thread (the service allows itself ten seconds for that), short enough that a dialog does not
#: look hung when the other machine is simply off.
TIMEOUT_SECONDS = 20.0

#: What the PACK request gets instead. It is not a timeout on a network round trip: the source
#: exports the whole selection, media and all, before writing a byte of the answer, so this is a
#: bound on how long an export may take. Fifteen minutes covers a large deck with its media on a
#: slow disk; past that something is genuinely wrong, and a machine that packs for longer than
#: the user will sit still for is not a machine this should keep waiting on.
PACK_TIMEOUT_SECONDS = 900.0

logger = get_logger("sync")

#: What a machine that is not sharing looks like on the wire: nothing is listening on the port,
#: so the OS refuses the connection outright.
_REFUSED = "Nothing is sharing on that machine right now — open Omnia there and turn sharing on."

#: One sentence for every way something answers that is not an Omnia — a router page, an ssh
#: banner, a connection reset. They differ on the wire and not in what the user should do.
_NOT_OMNIA = (
    "Something answered at that ID, but it was not Omnia. Check the ID, and that the other "
    "machine is sharing."
)


class SyncError(RuntimeError):
    """A pull that did not happen, carrying the sentence to show."""


class SyncClient:
    """Asks one source machine, identified by the address decoded from its ID."""

    def __init__(
        self, address: PairingAddress, *, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        self._address = address
        self._timeout = timeout

    def hello(self) -> dict[str, Any]:
        """Check that the other machine is there and accepts this ID.

        Returns:
            The machine's answer (``{"ok": True, "protocol": N}``).

        Raises:
            SyncError: With a sentence naming what to do about it — including when something
                answered 200 with a body that is not an Omnia's. The ID names a port, and a port
                on another machine can be held by anything: a router page, a dev server, another
                add-on. Letting a JSONDecodeError escape here breaks :func:`check`, whose whole
                promise is that the call site needs no exception handling.
        """
        body = self._get(HELLO_PATH)
        try:
            answer = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SyncError(_NOT_OMNIA) from exc
        if (
            not isinstance(answer, dict)
            or not isinstance(answer.get("protocol"), int)
            or isinstance(answer.get("protocol"), bool)
        ):
            # A body whose protocol is missing OR is not a number is not an old Omnia — it is
            # not an Omnia. An ID names a PORT, and a port on another machine can be held by
            # anything that answers JSON: a dev server, a router page, some other add-on's API.
            # Reading either as "protocol 0" and reporting "that machine runs an older Omnia"
            # sends the user off to update software that was never the problem, which is the one
            # thing this module promises not to do. `bool` is excluded because it is an `int` in
            # Python and `{"protocol": true}` is not a version.
            raise SyncError(_NOT_OMNIA)
        # Checked HERE so the Check button is where a version mismatch is reported. Left to the
        # inventory pull, "it works" would appear first and the real answer only once the user
        # had gone on to choose what to copy.
        _check_protocol(_int(answer.get("protocol")))
        return answer

    def inventory(self) -> Inventory:
        """Ask what the other machine has.

        Returns:
            Its inventory.

        Raises:
            SyncError: On anything that stopped the answer arriving or being readable.
        """
        try:
            return Inventory.from_json(self._get(INVENTORY_PATH).decode("utf-8"))
        except InventoryError as exc:
            # Already a sentence about which machine to update — pass it straight through rather
            # than wrapping it in a second, vaguer one.
            raise SyncError(str(exc)) from exc

    def request_package(self, request: PackageRequest) -> PackageOffer:
        """Ask the other machine to pack a selection, and learn how big it is.

        Separate from fetching it so the size is known BEFORE a byte moves — which is what turns
        the copy into a percentage and a time estimate rather than a spinner that means nothing.

        Given :data:`PACK_TIMEOUT_SECONDS` rather than the usual one. Packing is SYNCHRONOUS on
        the source: it exports the whole selection, media and all, before writing a byte of the
        answer. Three decks with a few hundred megabytes of media is minutes, and the ordinary
        twenty-second deadline would report "it did not answer in time — it may be asleep", which
        is the one thing this module promises never to do: name a cause it has not established.
        Worse, the source would finish and hold a package nobody ever fetched, and every retry
        would leak another one.

        Returns:
            What it packed.

        Raises:
            SyncError: On anything that stopped the answer arriving or being readable.
        """
        body = request.to_json().encode("utf-8")
        try:
            return PackageOffer.from_json(
                self._send(PACKAGE_PATH, body, timeout=PACK_TIMEOUT_SECONDS).decode(
                    "utf-8"
                )
            )
        except PackageError as exc:
            raise SyncError(str(exc)) from exc

    def download_package(
        self,
        offer: PackageOffer,
        destination: str,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
        """Fetch a packed selection to ``destination``, reporting bytes as they arrive.

        Streamed to disk rather than read into memory: a few decks with their media is routinely
        hundreds of megabytes, and Anki's importer wants a path anyway.

        Args:
            offer: What the other machine said it packed.
            destination: Where to write it.
            on_progress: Called with the RUNNING TOTAL of bytes written, often. The caller decides
                how often to repaint; this one does not throttle, because a transfer that stalls
                should be visible as a number that stops moving.

        Returns:
            How many bytes arrived.

        Raises:
            SyncError: On anything that stopped the package arriving whole. A partial file is
                removed — an ``.apkg`` that is 90% of a package is not a smaller package, it is a
                file Anki will refuse, and leaving it behind invites a retry that "works".
        """
        url = f"{self._address.base_url}{PACKAGE_PATH}?id={quote(offer.id)}"
        request = urllib.request.Request(url, method="GET", headers=self._headers())
        received = 0
        try:
            with (
                urllib.request.urlopen(request, timeout=self._timeout) as response,
                open(destination, "wb") as handle,
            ):
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received)
        except Exception as exc:
            _discard(destination)
            raise SyncError(_transfer_failure(exc)) from None
        if offer.bytes and received != offer.bytes:
            _discard(destination)
            raise SyncError(
                "The copy stopped part way through. Nothing was added to this collection — "
                "try again."
            )
        return received

    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self._address.token, "User-Agent": "omnia-sync"}

    def _send(
        self, path: str, body: bytes, *, timeout: Optional[float] = None
    ) -> bytes:
        """One authenticated POST, with every failure turned into a sentence."""
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._address.base_url}{path}", data=body, method="POST", headers=headers
        )
        return self._perform(request, timeout=timeout)

    def _get(self, path: str) -> bytes:
        """One authenticated GET, with every failure turned into a sentence."""
        request = urllib.request.Request(
            f"{self._address.base_url}{path}", method="GET", headers=self._headers()
        )
        return self._perform(request)

    def _perform(
        self, request: urllib.request.Request, *, timeout: Optional[float] = None
    ) -> bytes:
        """Send one prepared request, with every failure turned into a sentence."""
        deadline = timeout or self._timeout
        try:
            with urllib.request.urlopen(request, timeout=deadline) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise SyncError(_from_status(exc.code, _reason(exc))) from None
        except urllib.error.URLError as exc:
            raise SyncError(_from_url_error(exc.reason)) from None
        except TimeoutError:
            raise SyncError(
                "The other machine did not answer in time. It may be asleep, or busy with a "
                "long sync of its own."
            ) from None
        except (http.client.HTTPException, OSError) as exc:
            # `urllib` wraps only what `h.request()` raises; `h.getresponse()` is outside that
            # guard, so a peer that answers with a non-HTTP banner (an ssh daemon on a stale
            # port) or accepts and resets comes through here unwrapped. An ID carries a PORT, so
            # "something else has that port now" is an ordinary setup mistake — and inside Anki
            # an exception escaping a QueryOp callback pops the error dialog, which is the exact
            # failure the non-ASCII key fix just closed.
            logger.debug("sync: %s answered unusably: %r", self._address.host, exc)
            raise SyncError(_NOT_OMNIA) from None


def _discard(path: str) -> None:
    """Remove a half-written package.

    An ``.apkg`` that is 90% of a package is not a smaller package — it is a file Anki refuses —
    and leaving it on disk invites a retry that finds it already there and "works".
    """
    with contextlib.suppress(OSError):
        os.unlink(path)


def _transfer_failure(exc: BaseException) -> str:
    """What stopped a package arriving, as a sentence.

    Shares the wording of the smaller requests where the condition is the same, and adds the one
    thing that only matters here: nothing was added to this collection. A user who does not know
    that will go looking for a half-imported deck.
    """
    if isinstance(exc, SyncError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return _from_status(exc.code, _reason(exc))
    if isinstance(exc, urllib.error.URLError):
        return _from_url_error(exc.reason)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return (
            "The copy stopped part way through — the other machine stopped answering. Nothing "
            "was added to this collection."
        )
    if isinstance(exc, OSError) and getattr(exc, "filename", None):
        return f"Could not write the file: {exc.strerror or exc}."
    logger.debug("sync: the transfer failed: %r", exc)
    return "The copy stopped part way through. Nothing was added to this collection — try again."


def _reason(exc: urllib.error.HTTPError) -> str:
    """The server's own sentence, when it sent one worth showing."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def _from_status(status: int, reason: str) -> str:
    """What an HTTP status means here.

    The service's own sentence is preferred where it sent one: it knows things this side does
    not, and repeating it keeps one wording for one condition across both machines.
    """
    if status == 403:
        # The one status where THIS side knows more. The service answers "that ID does not open
        # this machine" and deliberately cannot say why — it must give one answer for a missing
        # key and a wrong one, or the difference tells a caller which half they got right. This
        # side knows there is a fix and what it is, so it says so; the server's own sentence
        # becomes the first half and never the whole of it.
        return (
            "That access code does not open the other machine. It may have been changed there "
            "— read it off that screen again."
        )
    if status == 429:
        return reason or (
            "The other machine has had too many wrong codes — wait a minute and try again."
        )
    if status == 410:
        # The endpoint is there and what it named is gone: a package already collected, or one
        # discarded when the other machine stopped sharing. Its own sentence, not "check both
        # are up to date" — the builds are fine.
        return reason or "That package is no longer waiting on the other machine."
    if status == 501:
        return reason or (
            "The other machine can list what it has but cannot copy it — update Omnia there."
        )
    if status == 404:
        return (
            "The other machine answered, but not as an Omnia that can share. Check both are "
            "up to date."
        )
    if status == 503:
        return reason or "The other machine is busy — try again in a moment."
    return reason or f"The other machine answered {status}."


def _from_url_error(reason: Any) -> str:
    """What a transport failure means here, judged by the underlying error.

    Deliberately distinguishes the three: refused means something answered and said no, a
    timeout means nothing answered at all, and a name that will not resolve is a broken ID.
    Reporting them as one "could not connect" is what leaves a user with nothing to do next.
    """
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return (
            "The other machine did not answer in time. It may be asleep, or on a network this "
            "one cannot reach."
        )
    if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
        return _REFUSED
    if isinstance(reason, socket.gaierror) or "name or service" in str(reason).lower():
        return "That ID does not point anywhere this machine can reach."
    return f"Could not reach the other machine: {reason}"


def _int(value: Any) -> int:
    """A protocol number, or 0 when the answer carried something else."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _check_protocol(protocol: int) -> None:
    """Raise when the other machine speaks a different protocol, naming which one to update.

    Same wording as :meth:`Inventory.from_json`, deliberately: one condition, one sentence,
    wherever the user happens to meet it.
    """
    if protocol > PROTOCOL:
        raise SyncError(
            "the other machine runs a newer Omnia than this one — update this machine "
            "before syncing from it"
        )
    if protocol < PROTOCOL:
        raise SyncError(
            "the other machine runs an older Omnia than this one — update that machine "
            "before syncing from it"
        )
