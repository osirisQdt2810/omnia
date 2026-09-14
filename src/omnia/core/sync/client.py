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

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from omnia.core.sync.inventory import Inventory, InventoryError
from omnia.core.sync.pairing import PairingAddress
from omnia.core.sync.service import HELLO_PATH, INVENTORY_PATH, TOKEN_HEADER

#: How long to wait on the other machine. Long enough for a collection read behind a busy Qt main
#: thread (the service allows itself ten seconds for that), short enough that a dialog does not
#: look hung when the other machine is simply off.
TIMEOUT_SECONDS = 20.0

#: What a machine that is not sharing looks like on the wire: nothing is listening on the port,
#: so the OS refuses the connection outright.
_REFUSED = "Nothing is sharing on that machine right now — open Omnia there and turn sharing on."


class SyncError(RuntimeError):
    """A pull that did not happen, carrying the sentence to show."""


class SyncClient:
    """Asks one source machine, identified by the address decoded from its ID."""

    def __init__(
        self, address: PairingAddress, *, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        self._address = address
        self._timeout = timeout

    def hello(self) -> dict:
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
            raise SyncError(
                "Something answered at that ID, but it was not Omnia. Check the ID, and that "
                "the other machine is sharing."
            ) from exc
        if not isinstance(answer, dict):
            raise SyncError(
                "Something answered at that ID, but it was not Omnia. Check the ID, and that "
                "the other machine is sharing."
            )
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

    def _get(self, path: str) -> bytes:
        """One authenticated GET, with every failure turned into a sentence."""
        request = urllib.request.Request(
            f"{self._address.base_url}{path}",
            method="GET",
            headers={TOKEN_HEADER: self._address.token, "User-Agent": "omnia-sync"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
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
        return reason or (
            "That ID does not open the other machine. It may have been regenerated there — "
            "copy it again."
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


def check(address: PairingAddress, *, timeout: Optional[float] = None) -> str:
    """Try an ID and return "" when it works, or the sentence explaining why it does not.

    The shape the dialog's Check button wants: no exception handling at the call site, and a
    single place where "it works" is defined as *the other machine answered our key*.
    """
    client = SyncClient(address, timeout=timeout or TIMEOUT_SECONDS)
    try:
        client.hello()
    except SyncError as exc:
        return str(exc)
    return ""
