"""The source machine's side of a sync: a read-only service the other machine pulls from.

ADR-020 allows this one listener off loopback, under five conditions, and they are all here
rather than in a document:

1. **Off until the user turns it on.** Nothing in this module binds by itself. :meth:`Session.start`
   is called by the settings UI when sharing is switched on, and a fresh profile has it off.
2. **The access code is the key.** Every request must carry it. A request without it is refused
   before anything reads the collection, and the comparison is constant-time so the answer's
   TIMING cannot be used to walk the code out one digit at a time. The code is nine digits — a
   number somebody reads off one screen and types into another — and nine digits are only safe
   because of :class:`_Lockout`: a few wrong ones and this machine stops answering for a while,
   which turns a billion combinations from an afternoon's work into centuries of it. The short
   code and the lockout are one design, and neither half is sound without the other.
3. **Read-only, always.** Two endpoints, both ``GET``. There is nothing here that writes, so the
   worst a peer can do to this machine is read what its owner published.
4. **The user only ever handles an ID.** This module never renders anything; the address and the
   key are folded into the ID by :mod:`omnia.core.sync.pairing`.
5. **It says what it is doing.** Every served request is logged with the endpoint and the peer.

The collection is Anki's, and Anki's collection is MAIN-THREAD ONLY, while every request arrives
on a worker thread. So the handler never touches it directly: it calls the ``inventory`` callable
it was constructed with, and the caller is responsible for marshalling that onto the Qt thread
(``anki_compat.run_on_main``) exactly as the lookup service does, with the deadline in
:data:`MAIN_THREAD_TIMEOUT_SECONDS`. A ``TimeoutError`` out of that callable becomes a 503, so the
other machine shows "busy" rather than hanging — but the deadline itself is the caller's, not
this module's.

No ``aqt``/``anki`` import here, which is what lets the whole service run in a test against a
plain function.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.sync.inventory import PROTOCOL, Inventory

logger = get_logger("sync")

HELLO_PATH = "/sync/hello"
INVENTORY_PATH = "/sync/inventory"

#: The header the machine's key travels in. A header rather than a query parameter: a URL ends up
#: in logs, in shell history and in a browser's address bar, and the key is the whole of the
#: access control.
TOKEN_HEADER = "X-Omnia-Sync-Key"

#: Wrong codes tolerated before this machine stops answering. Five is enough for somebody
#: fat-fingering a nine-digit number off another screen and nowhere near enough to search it.
MAX_ATTEMPTS = 5

#: How long a lockout lasts, and how long it takes to forget failures that stopped. A minute is
#: survivable for the person typing and ruinous for anyone guessing: five tries a minute against
#: a billion codes is a job measured in centuries.
LOCKOUT_SECONDS = 60.0

#: What the CALLER should give its collection read before giving up — this module does not
#: enforce it and must not be read as doing so. The service knows nothing about Anki: it calls
#: the ``inventory`` callable and turns a ``TimeoutError`` out of it into a 503. The deadline
#: belongs to whoever marshals onto the Qt thread, and this is the number to use, so both ends
#: of that contract are written in one place.
MAIN_THREAD_TIMEOUT_SECONDS = 10.0


class _Lockout:
    """Counts wrong codes, and shuts the door for a while once there are too many.

    Deliberately NOT per peer address. An attacker picks their source address and a per-address
    counter is one they can reset at will, which makes the limit decorative; this listener lives
    on a private network where the cost of the blunt version — anyone reachable can lock the
    owner out for a minute — is a nuisance, and the owner can see why in the log. A guessing
    limit that can be sidestepped is worse than none, because the nine-digit code was chosen on
    the assumption that this one holds.

    Thread-safe: requests arrive on a thread each.
    """

    def __init__(
        self,
        *,
        limit: int = MAX_ATTEMPTS,
        seconds: float = LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._until = 0.0

    @property
    def locked(self) -> bool:
        """Whether this machine is refusing everything right now."""
        with self._lock:
            return self._clock() < self._until

    def record_failure(self) -> bool:
        """Count one wrong code. Returns whether that closed the door."""
        with self._lock:
            now = self._clock()
            if now >= self._until and self._failures >= self._limit:
                self._failures = 0  # the last lockout expired; start counting again
            self._failures += 1
            if self._failures < self._limit:
                return False
            self._until = now + self._seconds
            return True

    def record_success(self) -> None:
        """Forget the failures — the right code arrived, so nobody is guessing."""
        with self._lock:
            self._failures = 0
            self._until = 0.0


class _Server(ThreadingHTTPServer):
    """A ThreadingHTTPServer whose bind honours "port taken -> fail" on every platform.

    The stdlib default sets ``SO_REUSEADDR``, which on **Windows** means something else entirely:
    there it lets a second socket take a port that is already being served, so a conflict would
    silently double-bind instead of being reported — hence off there.

    It stays on elsewhere because of what turning it off would cost: a socket that has just been
    closed sits in TIME_WAIT for up to a minute, and every restart this feature does — the "New
    ID" button, switching sharing off and on — reopens the same port immediately. Without the
    flag those would fail for a minute with "port in use", which is a lie about what is wrong.

    The flag does relax one more thing on BSD/macOS than on Linux: a wildcard bind can coexist
    with a bind to one specific address on the same port. That is not a hole here — every session
    binds the wildcard, so two of them still collide and the second is refused.
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class Session:
    """One machine's offer to be pulled from: a socket, a key, and a way to read the collection.

    Args:
        inventory: Returns what this machine offers. Called on a WORKER thread, so a caller that
            reads Anki must marshal onto the main thread inside this callable — the service
            cannot do it, because it deliberately knows nothing about Anki.
        key: The access code this machine shows beneath its ID.
        host: What to bind. ``0.0.0.0`` to be reachable from another machine, which is the point;
            a caller that wants loopback can pass it.
        port: What to bind.
    """

    def __init__(
        self,
        inventory: Callable[[], Inventory],
        *,
        key: str,
        host: str = "0.0.0.0",
        port: int = 8767,
    ) -> None:
        self._inventory = inventory
        self._key = key
        self._host = host
        self._port = port
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._lockout = _Lockout()

    @property
    def running(self) -> bool:
        """Whether the socket is open right now."""
        return self._server is not None

    @property
    def port(self) -> int:
        """The port actually bound — which is what the ID must carry when port 0 was asked for."""
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    def start(self) -> bool:
        """Open the socket. Returns whether it is now serving.

        A bind failure is reported, never raised: the port being taken is an ordinary state
        (another profile, another copy of Anki) and it must show up as "sharing could not start"
        in the dialog rather than as a traceback during a settings change.
        """
        if self._server is not None:
            return True
        if not self._key:
            logger.error("sync: refusing to serve without an access code")
            return False
        try:
            self._server = _Server((self._host, self._port), self._handler())
        except OSError as exc:
            logger.error(
                "sync: could not open the sharing port %s: %s", self._port, exc
            )
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="omnia-sync",
            daemon=True,
        )
        self._thread.start()
        logger.info("sync: sharing is on, port %s", self.port)
        return True

    def stop(self) -> None:
        """Close the socket. Safe to call when it was never open."""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        logger.info("sync: sharing is off")

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        """Build the request handler bound to this session."""
        session = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # A socket that opens and then says nothing would otherwise park a thread for ever
            # inside readline(). The server is threaded, so those accumulate — and `stop()`
            # cannot reclaim them: it closes the listening socket and joins serve_forever, while
            # a parked handler thread outlives the session and the switch that turned it off.
            # The lookup service carries the same five seconds for the same reason, and says
            # "any local process can start" one; this socket is reachable from another machine,
            # so it matters more here.
            timeout = 5

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path not in (HELLO_PATH, INVENTORY_PATH):
                    self._send(404, {"error": "no such endpoint"})
                    return
                if session._lockout.locked:
                    # Answered BEFORE the code is compared, so a locked-out guesser learns
                    # nothing from how long the answer took or from what it said.
                    logger.warning(
                        "sync: locked out, refused %s from %s", path, self._peer()
                    )
                    self._send(
                        429,
                        {"error": "too many wrong codes — wait a minute and try again"},
                    )
                    return
                if not session._authorised(self.headers.get(TOKEN_HEADER)):
                    # Deliberately the same answer for "no code" and "wrong code": telling them
                    # apart tells a caller which half they got right.
                    if session._lockout.record_failure():
                        logger.warning(
                            "sync: too many wrong codes from %s — refusing for %.0fs",
                            self._peer(),
                            LOCKOUT_SECONDS,
                        )
                    else:
                        logger.warning(
                            "sync: refused a request for %s from %s", path, self._peer()
                        )
                    self._send(403, {"error": "that code does not open this machine"})
                    return
                session._lockout.record_success()
                logger.info("sync: served %s to %s", path, self._peer())
                if path == HELLO_PATH:
                    self._send(200, {"ok": True, "protocol": PROTOCOL})
                    return
                self._send_inventory()

            def _send_inventory(self) -> None:
                try:
                    payload = session._inventory()
                except TimeoutError:
                    # The collection could not be reached in time — Anki is mid-sync, or a modal
                    # is up. A 503 is a state the other machine can render and retry.
                    self._send(503, {"error": "the other machine is busy"})
                except Exception:
                    logger.exception("sync: could not build the inventory")
                    self._send(
                        500, {"error": "the other machine could not list its decks"}
                    )
                else:
                    self._send_raw(200, payload.to_json().encode("utf-8"))

            def _send(self, status: int, body: dict[str, Any]) -> None:
                self._send_raw(status, json.dumps(body).encode("utf-8"))

            def _send_raw(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                # No CORS header, deliberately: a web page has no business reaching this, and
                # the browser refusing it is one more thing that has to go wrong first.
                self.end_headers()
                self.wfile.write(body)

            def _peer(self) -> str:
                return str(self.client_address[0]) if self.client_address else "?"

            def log_message(self, *_args: Any) -> None:
                """Silence the stdlib's stderr logging; this service logs its own lines."""

        return Handler

    def _authorised(self, presented: Optional[str]) -> bool:
        """Whether a request carried this machine's access code.

        ``compare_digest`` rather than ``==``: a plain comparison returns as soon as two bytes
        differ, and the time it took is a measurement anyone on the network can make — enough to
        walk the key out one byte at a time given enough requests.

        Compared as BYTES because ``compare_digest`` REJECTS a non-ASCII ``str`` outright, and
        the header is whatever a peer chose to send (HTTP headers decode as latin-1, so any byte
        ≥ 0x80 gets there). As a str comparison it raised TypeError from inside the handler,
        which cost three things at once: the request got no answer at all — so "no key" and
        "wrong key" were no longer indistinguishable, a probe could tell non-ASCII apart from
        everything else; the refusal was never logged; and the traceback went to stderr, which
        inside Anki pops its error dialog. One stray probe from the network, one dialog. The
        lookup service solved this first and wrote down why.
        """
        if not presented:
            return False
        return hmac.compare_digest(
            self._key.encode("utf-8"), str(presented).encode("utf-8")
        )
