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
3. **Nothing a peer sends can change this collection.** Three endpoints. Two are ``GET``; the
   third is a ``POST`` that asks for a selection to be packed, and it is a POST because the
   selection is a list of deck names that does not belong in a URL. It still only READS: it
   exports to a temp file and hands the bytes over. Nothing in this module writes to the
   collection, and the worst a peer can do to this machine is read what its owner published.
   (ADR-021 restates ADR-020's original "both GET" as this, which is what it was protecting.)
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
from urllib.parse import parse_qs, urlparse

from omnia.core.logging import get_logger
from omnia.core.sync.inventory import PROTOCOL, Inventory
from omnia.core.sync.package import PackageError, PackageRequest

logger = get_logger("sync")

HELLO_PATH = "/sync/hello"
INVENTORY_PATH = "/sync/inventory"
#: Ask the source to pack a selection (POST), then fetch the bytes (GET). Two steps rather than
#: one so the target learns the SIZE before a byte moves — which is what turns the copy into a
#: percentage and a time rather than a spinner.
PACKAGE_PATH = "/sync/package"

#: The header the machine's key travels in. A header rather than a query parameter: a URL ends up
#: in logs, in shell history and in a browser's address bar, and the key is the whole of the
#: access control.
TOKEN_HEADER = "X-Omnia-Sync-Key"

#: The biggest request body this will read. A selection is a list of deck names; a megabyte is
#: thousands of them, and anything larger is a denial of service rather than a choice.
_MAX_REQUEST_BYTES = 1_000_000

#: How much of a package is read and written at a time. Small enough that a 700 MB transfer is
#: not held in memory, big enough that it is not a syscall per kilobyte.
_CHUNK_BYTES = 256 * 1024

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
        self._last_failure = 0.0

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
            elif self._failures and now - self._last_failure >= self._seconds:
                # Failures that STOPPED are forgotten. Without this the count only ever went up:
                # four typos spread over a week and the fifth, months later, locks the machine
                # out — which is not a guessing run, and the limit exists for guessing runs.
                self._failures = 0
            self._last_failure = now
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
            self._last_failure = 0.0


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
        packager: Packs a :class:`~omnia.core.sync.package.PackageRequest` and returns
            ``(path, offer)``. Also called on a worker thread, and it is meant to be: Anki's own
            exporter runs on one (``aqt.import_export.exporting`` hands ``col`` to a ``QueryOp``),
            and packing a few hundred megabytes of media on the Qt thread would freeze the source
            machine for minutes. ``None`` leaves this machine able to list what it has and unable
            to hand any of it over, which is what an older build looks like to a newer peer.
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
        packager: Optional[Callable[[Any], tuple[str, Any]]] = None,
    ) -> None:
        self._inventory = inventory
        self._packager = packager
        # Packages waiting to be collected, by id. Held so the bytes can be served by a second
        # request and cleaned up afterwards — an export nobody fetched must not outlive the
        # session, or a machine that shares once leaks hundreds of megabytes of temp file.
        self._packages: dict[str, str] = {}
        self._packages_lock = threading.Lock()
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
        self._discard_packages()
        logger.info("sync: sharing is off")

    def _discard_packages(self) -> None:
        """Delete every package still waiting to be collected.

        On stop rather than only after a successful send: a peer that asks for a package and then
        closes its laptop leaves one behind, and hundreds of megabytes of temp file per attempt is
        not an acceptable way to find that out.
        """
        with self._packages_lock:
            paths, self._packages = list(self._packages.values()), {}
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                logger.warning("sync: could not remove the package at %s", path)

    def _take_package(self, package_id: str) -> Optional[str]:
        """The path for ``package_id``, removed from the waiting list. One fetch per package."""
        with self._packages_lock:
            return self._packages.pop(package_id, None)

    def _hold_package(self, package_id: str, path: str) -> None:
        with self._packages_lock:
            self._packages[package_id] = path

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

            def do_POST(self) -> None:
                """Ask for a selection to be packed. Reads the collection; never writes to it."""
                path = self.path.split("?", 1)[0]
                if path != PACKAGE_PATH:
                    self._send(404, {"error": "no such endpoint"})
                    return
                if not self._allowed(path):
                    return
                self._pack()

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == PACKAGE_PATH:
                    if self._allowed(path):
                        self._send_package()
                    return
                if path not in (HELLO_PATH, INVENTORY_PATH):
                    self._send(404, {"error": "no such endpoint"})
                    return
                if not self._allowed(path):
                    return
                if path == HELLO_PATH:
                    self._send(200, {"ok": True, "protocol": PROTOCOL})
                    return
                self._send_inventory()

            def _allowed(self, path: str) -> bool:
                """Whether this request may proceed. Answers the refusal itself when not."""
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
                    return False
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
                    return False
                session._lockout.record_success()
                logger.info("sync: served %s to %s", path, self._peer())
                return True

            def _pack(self) -> None:
                """Export what the peer asked for, and answer with its id and size."""
                if session._packager is None:
                    self._send(
                        501,
                        {
                            "error": (
                                "the other machine can list what it has but cannot copy it — "
                                "update Omnia there"
                            )
                        },
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > _MAX_REQUEST_BYTES:
                    # A body with no length, or one big enough to be a denial of service rather
                    # than a deck list. 1 MB is thousands of deck names.
                    self._send(400, {"error": "that request was not a selection"})
                    return
                body = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    request = PackageRequest.from_json(body)
                except PackageError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                try:
                    path, offer = session._packager(request)
                except TimeoutError:
                    self._send(503, {"error": "the other machine is busy"})
                    return
                except Exception:
                    logger.exception("sync: could not pack the selection")
                    self._send(
                        500, {"error": "the other machine could not pack that up"}
                    )
                    return
                session._hold_package(offer.id, path)
                logger.info(
                    "sync: packed %s (%s bytes) for %s",
                    offer.id,
                    offer.bytes,
                    self._peer(),
                )
                self._send_raw(200, offer.to_json().encode("utf-8"))

            def _send_package(self) -> None:
                """Stream one packed selection, then delete it. One fetch per package."""
                query = parse_qs(urlparse(self.path).query)
                package_id = (query.get("id") or [""])[0]
                path = session._take_package(package_id)
                if not path or not os.path.exists(path):
                    # 410 rather than 404: the endpoint exists and the thing it named is gone,
                    # which is a different sentence from "this build has no such endpoint" — and
                    # a 404 here made a collected package read as "the two Omnias differ".
                    self._send(410, {"error": "that package is no longer waiting"})
                    return
                try:
                    size = os.path.getsize(path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with open(path, "rb") as handle:
                        while True:
                            chunk = handle.read(_CHUNK_BYTES)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    logger.info("sync: sent %s (%s bytes)", package_id, size)
                finally:
                    # Whether it arrived or the peer vanished mid-transfer: this machine is not
                    # keeping a copy on the chance somebody asks again.
                    try:
                        os.unlink(path)
                    except OSError:
                        logger.warning("sync: could not remove the package at %s", path)

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
