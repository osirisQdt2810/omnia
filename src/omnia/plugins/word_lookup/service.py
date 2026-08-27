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
from pathlib import Path, PurePosixPath, PureWindowsPath
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
_MEDIA_PATH = "/media"


# Enough to let a browser or Qt identify the bytes; anything else is served as octet-stream,
# which QPixmap sniffs perfectly well.
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def _is_bare_file_name(name: str) -> bool:
    """Is ``name`` a plain file name, the only shape an Anki media reference takes?

    Anything carrying a separator, a drive or a parent reference is not one, and honouring it
    would turn a loopback lookup service into a file reader for the whole disk. Both flavours
    of path are asked, because the name arrives over HTTP from a client that may not run on
    this OS: a backslash means nothing to POSIX but everything to Windows.

    Asked this way rather than by searching for ".." as a SUBSTRING, which also refuses a
    legitimately-named ``diagram..png`` sitting in the media folder.
    """
    if name in {".", ".."}:
        return False
    return PurePosixPath(name).name == name and PureWindowsPath(name).name == name


class LookupService:
    """Serves ``GET /lookup?word=…`` and ``GET /media?file=…`` on loopback.

    The lookup callable is injected, so the whole service tests without Anki: pass any
    ``(word) -> dict``.

    ``/media`` exists because the panel this serves shows a note's images, and the bytes have
    to come from somewhere. They used to come from AnkiConnect, a SEPARATE add-on the user may
    simply not have -- and on a machine without it every preview reported "Image unavailable"
    while the lookup itself worked perfectly, because the lookup is this service and the image
    was not. This service already runs inside Anki with the collection open; serving the file
    is a few lines, and it removes a dependency the feature never needed.
    """

    def __init__(
        self,
        lookup: Callable[[str], dict[str, Any]],
        *,
        media_dir: Optional[Callable[[], str]] = None,
        port: int = 8766,
        host: str = "127.0.0.1",
        run_on_main: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        """Initialise the service (does not bind until :meth:`start`).

        Args:
            lookup: Performs one lookup and returns the JSON-able payload. Called on the Qt
                main thread when ``run_on_main`` is supplied.
            media_dir: Returns the collection's media folder. ``None`` disables ``/media``,
                which is what a headless test wants -- and what the panel reads as "no image
                fetcher", so it shows a badge instead of a broken button.
            port: Loopback port to listen on.
            host: Interface to bind. Anything but a loopback address is refused by
                :meth:`start` — this service must never be exposed to a network.
            run_on_main: Marshals a callable onto the Qt main thread. ``None`` runs the lookup
                inline (tests / headless), which is only safe when there is no live collection.
        """
        self._lookup = lookup
        self._media_dir = media_dir
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

    def _media_bytes(self, filename: str) -> Optional[bytes]:
        """Return a collection-media file's bytes, or ``None``.

        The name is checked BEFORE the folder is consulted: an Anki media name is a bare file
        name, so anything with a separator or a parent reference is not one, and honouring it
        would turn a loopback lookup service into a reader for the whole disk. The resolved
        path is then required to sit inside the media folder, which catches whatever the name
        check did not.
        """
        if self._media_dir is None or not filename:
            return None
        if not _is_bare_file_name(filename):
            return None
        try:
            # The RESULT decides, not the callable. The plugin returns "" when there is no
            # collection -- and every failure inside it funnels into that same "" -- while
            # Path("").resolve() is the process's working directory. Checking only that the
            # callable exists therefore turned "no media folder" into "serve Anki's CWD",
            # which is a real loopback file reader over a directory nobody chose.
            folder_name = self._media_dir()
            if not folder_name:
                return None
            folder = Path(folder_name).resolve()
            target = (folder / filename).resolve()
            if folder not in target.parents:
                return None
            return target.read_bytes()
        except (OSError, ValueError):
            # ValueError is the stdlib's signal for a path string it cannot use at all --
            # "embedded null byte" from resolve()/read_bytes(). It is NOT an OSError, and a
            # name that is nothing but a NUL passes the bare-name check, so it is the one
            # rejected shape that reaches the filesystem call instead of being refused
            # before it. Escaping here would print a traceback to stderr, which Anki turns
            # into an error dialog.
            return None

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
                route = parsed.path.rstrip("/")
                if route == _MEDIA_PATH:
                    self._serve_media(parse_qs(parsed.query))
                    return
                if route != _LOOKUP_PATH:
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

            def _serve_media(self, params: dict[str, list[str]]) -> None:
                """Answer ``GET /media?file=…`` with the raw bytes, or a JSON error."""
                filename = (params.get("file") or [""])[0].strip()
                if not filename:
                    self._respond(400, {"error": "missing 'file'"})
                    return
                # No main-thread hop: this is file IO plus one cached attribute read
                # (MediaManager.dir() returns a value set in __init__, with no backend call),
                # not a collection query. Marshalling it would queue behind whatever the user
                # is doing in Anki for no reason.
                #
                # Nothing may escape this method. Anki turns anything on stderr into its error
                # dialog, and socketserver prints an unhandled handler exception there -- so a
                # single bad request would pop a dialog mid-review, from any page that can do
                # <img src="http://127.0.0.1:.../media?file=...">. The /lookup branch above has
                # always caught broadly for this reason; this one now matches it.
                try:
                    data = service._media_bytes(filename)
                except Exception:
                    logger.exception("word_lookup: media failed for %r", filename)
                    self._respond(500, {"error": "media failed"})
                    return
                if data is None:
                    self._respond(404, {"error": "no such media file"})
                    return
                self._respond_bytes(
                    data,
                    _MEDIA_TYPES.get(
                        Path(filename).suffix.lower(), "application/octet-stream"
                    ),
                )

            def _respond_bytes(self, body: bytes, content_type: str) -> None:
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError as exc:
                    logger.debug("word_lookup: client disconnected mid-media (%s)", exc)

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
