"""The loopback lookup service: how the desktop clipper asks Anki about a word.

Why a service at all
--------------------
The companion desktop clipper is a *separate process* floating over whatever app the user is
reading. It can reach Anki through AnkiConnect, but AnkiConnect only returns raw notes — the
part worth owning centrally (which note types are searchable, which of a 35-field note type to
show, how hits are ranked) lives here in :mod:`~omnia.plugins.word_lookup.logic`. So omnia
exposes ONE read-only endpoint and the clipper stays a thin renderer.

Two hard constraints shape this module:

* **Loopback only.** The socket binds ``127.0.0.1`` and answers a single ``GET /lookup``. It is
  never reachable off the machine, and it can only read — there is no write path.
* **Anki's collection is main-thread-only.** The HTTP handler runs on a worker thread, so it
  must NOT touch ``mw.col`` directly (doing so corrupts state / crashes Qt). Every request
  marshals its collection read onto the Qt main thread via
  :func:`~omnia.core.anki_compat.run_on_main` and waits for the answer with a timeout, so a
  wedged main thread degrades to an error response instead of hanging the clipper.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from omnia.core.logging import get_logger

logger = get_logger("word_lookup")


class _ExclusiveBindHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer whose bind honors "port taken -> fail" on every platform.

    The stdlib default ``allow_reuse_address = 1`` sets SO_REUSEADDR, which on Windows
    lets a second socket bind a port that is already being served — a conflict would
    silently double-bind instead of reporting False. POSIX keeps the reuse flag (there
    it only relaxes TIME_WAIT, never an active listener).
    """

    allow_reuse_address = os.name != "nt"


# A request that cannot get the Qt main thread within this long is reported as an error rather
# than left hanging (the clipper shows "Anki is busy" instead of a spinner that never resolves).
_MAIN_THREAD_TIMEOUT_SECONDS = 5.0
_LOOKUP_PATH = "/lookup"


class LookupService:
    """Serves ``GET /lookup?word=…`` on loopback, answering from the collection.

    The lookup callable is injected, so the whole service tests without Anki: pass any
    ``(word) -> dict``.
    """

    def __init__(
        self,
        lookup: Callable[[str], dict[str, Any]],
        *,
        port: int = 8766,
        host: str = "127.0.0.1",
        run_on_main: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        """Initialise the service (does not bind until :meth:`start`).

        Args:
            lookup: Performs one lookup and returns the JSON-able payload. Called on the Qt
                main thread when ``run_on_main`` is supplied.
            port: Loopback port to listen on.
            host: Interface to bind. Anything but a loopback address is refused by
                :meth:`start` — this service must never be exposed to a network.
            run_on_main: Marshals a callable onto the Qt main thread. ``None`` runs the lookup
                inline (tests / headless), which is only safe when there is no live collection.
        """
        self._lookup = lookup
        self._port = port
        self._host = host
        self._run_on_main = run_on_main
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        """Whether the socket is currently bound and serving."""
        return self._server is not None

    def start(self) -> bool:
        """Bind and serve in a daemon thread. Returns whether the service came up.

        A failure to bind (port already taken, sandbox) is logged and reported, never raised:
        the lookup feature degrading to "unavailable" must not break enabling the plugin.
        """
        if self._server is not None:
            return True
        if not self._is_loopback(self._host):
            logger.error(
                "word_lookup: refusing to bind non-loopback host %r", self._host
            )
            return False
        try:
            self._server = _ExclusiveBindHTTPServer(
                (self._host, self._port), self._build_handler()
            )
        except OSError:
            logger.exception(
                "word_lookup: could not bind %s:%s", self._host, self._port
            )
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="omnia-word-lookup",
            daemon=True,  # never keeps Anki alive at quit
        )
        self._thread.start()
        logger.info("word_lookup: serving on %s:%s", self._host, self._port)
        return True

    def stop(self) -> None:
        """Shut the socket down and join the serving thread (safe to call when not running)."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logger.exception("word_lookup: error shutting the service down")
        if thread is not None:
            thread.join(timeout=2.0)

    @staticmethod
    def _is_loopback(host: str) -> bool:
        """Whether ``host`` is a loopback address (the only thing this service may bind)."""
        return host in {"127.0.0.1", "::1", "localhost"}

    def _lookup_via_main_thread(self, word: str) -> dict[str, Any]:
        """Run the injected lookup on the Qt main thread and return its payload.

        Anki's collection may only be touched from the main thread, but this runs on an HTTP
        worker thread — so the work is handed over and awaited. A main thread that never gets
        round to it raises, which the handler turns into a 503.

        Raises:
            TimeoutError: The main thread did not run the work in time.
        """
        if self._run_on_main is None:
            return self._lookup(word)  # headless/tests: no Qt loop to marshal onto
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["value"] = self._lookup(word)
            except Exception as exc:  # carried back to the requesting thread
                box["error"] = exc
            finally:
                done.set()

        self._run_on_main(work)
        if not done.wait(_MAIN_THREAD_TIMEOUT_SECONDS):
            raise TimeoutError("Anki's main thread did not answer the lookup in time")
        if "error" in box:
            raise box["error"]
        return box.get("value", {})

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        """Return a request-handler class bound to this service instance."""
        service = self

        class Handler(BaseHTTPRequestHandler):
            # Quiet: BaseHTTPRequestHandler logs every request to stderr, and writing to Anki's
            # stderr pops its error dialog.
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") != _LOOKUP_PATH:
                    self._respond(404, {"error": "unknown endpoint"})
                    return
                params = parse_qs(parsed.query)
                word = (params.get("word") or [""])[0].strip()
                if not word:
                    self._respond(400, {"error": "missing 'word'"})
                    return
                try:
                    payload = service._lookup_via_main_thread(word)
                except TimeoutError as exc:
                    self._respond(503, {"error": str(exc)})
                except Exception:
                    logger.exception("word_lookup: lookup failed for %r", word)
                    self._respond(500, {"error": "lookup failed"})
                else:
                    self._respond(200, payload)

            def _respond(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError as exc:
                    # The client hung up before we finished writing — an ordinary event for a
                    # floating clipper the user closes or that times out mid-lookup. Left to
                    # propagate it reaches socketserver's handle_error, which prints the
                    # traceback to stderr, and Anki turns anything on stderr into its ERROR
                    # DIALOG: a normal disconnect would interrupt the user's review. Same
                    # reason ``log_message`` above is silenced; this is the write path.
                    logger.debug(
                        "word_lookup: client disconnected mid-response (%s)", exc
                    )

        return Handler
