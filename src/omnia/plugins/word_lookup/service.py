"""The loopback lookup service: how the desktop clipper asks Anki about a word.

Why a service at all
--------------------
The companion desktop clipper is a *separate process* floating over whatever app the user is
reading. It can reach Anki through AnkiConnect, but AnkiConnect only returns raw notes — the
part worth owning centrally (which note types are searchable, which of a 35-field note type to
show, how hits are ranked) lives here in :mod:`~omnia.plugins.word_lookup.logic`. So omnia
exposes ONE read-only endpoint and the clipper stays a thin renderer.

Three hard constraints shape this module:

* **Loopback only.** The socket binds ``127.0.0.1``; it is never reachable off the machine.
* **Anki's collection is main-thread-only.** The HTTP handler runs on a worker thread, so it
  must NOT touch ``mw.col`` directly (doing so corrupts state / crashes Qt). Every request
  marshals its collection read onto the Qt main thread via
  :func:`~omnia.core.anki_compat.run_on_main` and waits for the answer with a timeout, so a
  wedged main thread degrades to an error response instead of hanging the clipper.
* **One endpoint WRITES.** ``POST /generate`` re-generates note fields through smart_notes:
  it overwrites the user's note content and spends their LLM/TTS credits. The reads
  (``/lookup``, ``/media``) stay open; the write path is guarded twice, and both guards matter.

Guarding the write path
-----------------------
There used to be two guards. The token is gone — removed at the owner's instruction after it
produced a steady stream of 401s and nothing else: it had to be copied by hand from a config
dialog into each clipper's options, it silently invalidated every clipper when rotated, and the
clippers reported a mistyped one as "Omnia rejected the access token" with no way to tell that
from a service that was simply not running.

What that costs, stated plainly: any process on this machine can now ask for a field to be
regenerated, which rewrites a note and spends LLM credits. What it does not cost is the thing the
token was mostly protecting against, because the remaining guard covers it:

**No request a web page started.** A ``POST`` whose ``Origin`` is a web origin
   (``http://…``/``https://…``) is refused outright. This service
   sends no CORS headers, so a page cannot READ an answer — but ``fetch(url, {mode:
   "no-cors"})`` still performs the SIDE EFFECT, and here the side effect rewrites note fields
   and spends money.

   The rule judges the VALUE, not the presence, because a browser extension's request carries
   an ``Origin`` too:

   * the Fetch spec appends ``Origin`` to every request whose method is not GET/HEAD, so a
     ``POST`` from the web clipper's service worker always has one;
   * ``host_permissions`` waives Chrome's check on the RESPONSE; it does not strip the header
     from the request (which is why the same worker needs the extension's origin in
     AnkiConnect's ``webCorsOriginList``, while the desktop clipper's ``urllib`` — sending no
     ``Origin`` at all — needs no such setting);
   * ``Origin`` is a forbidden header name: page JS can neither set nor remove it, and the
     browser fills in the page's own origin, so a page cannot disguise itself as an extension.

So: absent (a native client) or ``chrome-extension://…`` (an extension) is allowed; anything else
is a page and is refused — including ``http://127.0.0.1``, since a page served from localhost is
still a page. The allowance is by SCHEME, not by extension id: the id differs between an unpacked
dev load and a Web Store install, and pinning a value we do not control would break the clipper
for no gain.

The socket is loopback-only, so "any process on this machine" is the whole of the exposure, and
no web page can reach it however it is coaxed.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional, TypeVar
from urllib.parse import parse_qs, urlparse

from omnia.core.logging import get_logger

logger = get_logger("word_lookup")

_T = TypeVar("_T")


class PhraseCheckUnavailableError(RuntimeError):
    """Phrase Check is not running, so a phrase cannot be corrected.

    Distinct from a check that FAILED: this one is answered by switching a feature on, and the
    two reaching the clipper as one message is how a user ends up looking for a setting that was
    never the problem.
    """


class CheckFailedError(RuntimeError):
    """A phrase that could not be corrected, carrying the reason as a user-facing sentence."""


class RegenerationUnavailableError(RuntimeError):
    """Nothing can regenerate right now — smart_notes is off, or this build has no seam.

    Answered as ``503``: the client should tell the user the feature is unavailable and try
    again later, not that they did something wrong.
    """


class RegenerationDisabledError(RuntimeError):
    """smart_notes is there, but the user switched clipper regeneration off.

    Answered as ``409``, carrying the message the clipper shows verbatim — it names the option
    to turn back on, which no client should have to guess at.
    """


def _json_object(raw: bytes) -> dict[str, Any]:
    """One request body as a JSON object.

    Raises:
        _BadRequestError: When it is not one. Shared by both POST endpoints rather than written
            twice — the same two mistakes (not JSON, JSON but not an object) deserve the same two
            sentences wherever they are made.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _BadRequestError("body must be JSON") from exc
    if not isinstance(body, dict):
        raise _BadRequestError("body must be a JSON object")
    return body


class _BadRequestError(ValueError):
    """A request this service refuses to interpret; its message becomes the ``400`` body."""


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
_GENERATE_PATH = "/generate"
#: Corrects a phrase. A POST because the phrase is a paragraph, not a query parameter, and
#: because it spends LLM credits — so it sits behind the same page-origin refusal as /generate.
_CHECK_PATH = "/check"
#: The most a clipper may ask to have corrected in one request.
#:
#: The HTTP contract, checked here so that too much text is a 400 — the request being wrong —
#: rather than a 502 dressed as a provider failure, which would send the user to check an API
#: key over a selection that was merely too big. ``phrase_check`` keeps its own, independent
#: guard for direct callers; this one is what a clipper is held to, and the two are allowed to
#: differ (the stricter always wins, and that is the whole interaction between them).
MAX_PHRASE_CHARS = 2000
# The one non-empty Origin the write path accepts. By SCHEME, never by extension id: the id
# differs between an unpacked dev load and a Web Store install.
# ponytail: Chrome only -- a Firefox/Safari port of the clipper would send moz-extension:// or
# safari-web-extension://, and each is a one-line addition here when that port exists.
_EXTENSION_ORIGIN_SCHEME = "chrome-extension://"
# A generate request is a small JSON object (a note id and a few field names). The cap is what
# an unauthenticated caller may make this process buffer, so it is checked BEFORE reading.
_MAX_BODY_BYTES = 64 * 1024


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


def is_page_origin(origin: Optional[str]) -> bool:
    """Whether ``origin`` says a WEB PAGE started this request, which may not write.

    Judged by value, not by presence: a browser extension's ``POST`` carries an ``Origin`` too
    (the Fetch spec appends one to every non-GET/HEAD request), so refusing the header outright
    would refuse the web clipper. See the module docstring for why a page cannot forge this.

    Args:
        origin: The request's ``Origin`` header, or ``None``/``""`` when it sent none — which is
            what a native client such as the desktop clipper's ``urllib`` does.

    Returns:
        True for a web origin (``https://a-page.example``, and ``http://127.0.0.1`` too: a page
        served from localhost is still a page). False for no origin and for an extension's.
    """
    if not origin:
        return False
    return not origin.strip().lower().startswith(_EXTENSION_ORIGIN_SCHEME)


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
    """Serves ``GET /lookup``, ``GET /media`` and ``POST /generate`` on loopback.

    Every collaborator is injected, so the whole service tests without Anki: pass any
    ``(word, client) -> dict``.

    ``/media`` exists because the panel this serves shows a note's images, and the bytes have
    to come from somewhere. They used to come from AnkiConnect, a SEPARATE add-on the user may
    simply not have -- and on a machine without it every preview reported "Image unavailable"
    while the lookup itself worked perfectly, because the lookup is this service and the image
    was not. This service already runs inside Anki with the collection open; serving the file
    is a few lines, and it removes a dependency the feature never needed.
    """

    def __init__(
        self,
        lookup: Callable[[str, str], dict[str, Any]],
        *,
        media_dir: Optional[Callable[[], str]] = None,
        generate: Optional[
            Callable[[str, int, Optional[list[str]]], dict[str, Any]]
        ] = None,
        check: Optional[Callable[[str, str, bool], dict[str, Any]]] = None,
        port: int = 8766,
        host: str = "127.0.0.1",
        run_on_main: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        """Initialise the service (does not bind until :meth:`start`).

        Args:
            lookup: Performs one lookup for ``(word, client)`` and returns the JSON-able
                payload. Called on the Qt main thread when ``run_on_main`` is supplied.
            media_dir: Returns the collection's media folder. ``None`` disables ``/media``,
                which is what a headless test wants -- and what the panel reads as "no image
                fetcher", so it shows a badge instead of a broken button.
            generate: Regenerates fields for ``(client, note_id, fields)`` and returns the
                JSON-able payload; ``fields=None`` means every field. Called on the HTTP worker
                thread, NOT marshalled: generation talks to LLM/TTS providers for far longer
                than the main thread may be held, so it does its own marshalling for the parts
                that need the collection. ``None`` leaves ``POST /generate`` answering 503.
            check: Corrects a phrase for ``(text, mode, refresh)``. Called on the HTTP worker
                thread and never marshalled — it talks to an LLM, which the Qt thread may not be
                held for, and it touches no collection at all. ``None`` leaves ``POST /check``
                answering 503, which is what a build without Phrase Check looks like.
            port: Loopback port to listen on.
            host: Interface to bind. Anything but a loopback address is refused by
                :meth:`start` — this service must never be exposed to a network.
            run_on_main: Marshals a callable onto the Qt main thread. ``None`` runs the lookup
                inline (tests / headless), which is only safe when there is no live collection.
        """
        self._lookup = lookup
        self._media_dir = media_dir
        self._generate = generate
        self._check = check
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

    def call_on_main(self, work: Callable[[], _T]) -> _T:
        """Run ``work`` on the Qt main thread and return its result.

        Anki's collection may only be touched from the main thread, while everything here runs
        on an HTTP worker thread — so the call is handed over and awaited. A main thread that
        never gets round to it raises, which the handlers turn into a 503.

        Public because the collection read that follows a regeneration needs exactly this, and
        one implementation of "hop to the main thread and wait" is the point: a second copy
        would be a second timeout policy to keep in step with this one.

        Raises:
            TimeoutError: The main thread did not run the work in time.
        """
        if self._run_on_main is None:
            return work()  # headless/tests: no Qt loop to marshal onto
        box: dict[str, Any] = {}
        done = threading.Event()

        def runner() -> None:
            try:
                box["value"] = work()
            except Exception as exc:  # carried back to the requesting thread
                box["error"] = exc
            finally:
                done.set()

        self._run_on_main(runner)
        if not done.wait(_MAIN_THREAD_TIMEOUT_SECONDS):
            raise TimeoutError("Anki's main thread did not answer in time")
        if "error" in box:
            raise box["error"]
        return box["value"]  # type: ignore[no-any-return]

    def _lookup_via_main_thread(self, word: str, client: str) -> dict[str, Any]:
        """Run the injected lookup for ``client`` on the Qt main thread and return its payload."""
        return self.call_on_main(lambda: self._lookup(word, client))

    def _regenerate(
        self, client: str, note_id: int, fields: Optional[list[str]]
    ) -> dict[str, Any]:
        """Run the injected generate callable ON THIS (worker) THREAD and return its payload.

        Deliberately not marshalled: a generation calls LLM/TTS providers, which is orders of
        magnitude longer than the Qt main thread may be held, and the 5 s budget the reads use
        would expire long before an answer. The callable marshals the parts that touch the
        collection itself. Blocking here only blocks THIS request — the server threads each
        connection, so a lookup arriving mid-generation is answered straight away.

        Raises:
            RegenerationUnavailableError: No generate callable was injected.
        """
        if self._generate is None:
            raise RegenerationUnavailableError("this service cannot regenerate fields")
        return self._generate(client, note_id, fields)

    def _check_phrase(self, text: str, mode: str, refresh: bool) -> dict[str, Any]:
        """Correct one phrase through whatever was injected.

        Raises:
            PhraseCheckUnavailableError: When nothing is wired — Phrase Check is switched off, or
                this build has none. Answered by switching a feature on, which is a different
                sentence from a check that was attempted and failed.
            CheckFailedError: When the check ran and could not finish.
        """
        if self._check is None:
            raise PhraseCheckUnavailableError(
                "Phrase Check is switched off in Omnia — turn it on to correct a phrase"
            )
        try:
            return self._check(text, mode, refresh)
        except (PhraseCheckUnavailableError, CheckFailedError):
            raise
        except Exception as exc:
            # Whatever the plugin raised carries the user-facing sentence; it is re-labelled
            # rather than re-worded, so the reason the provider gave survives to the panel.
            raise CheckFailedError(str(exc) or "the check could not finish") from exc

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        """Return a request-handler class bound to this service instance."""
        service = self

        class Handler(BaseHTTPRequestHandler):
            # A socket that opens and then says nothing would otherwise park a thread for ever
            # inside readline(); the server is threaded, so a handful of those is a slow leak
            # any local process can start. Five seconds is far longer than a loopback client
            # needs to finish a request line, and it bounds nothing a real one does — the
            # generation itself happens after the request is fully read.
            timeout = 5

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
                # An older clipper sends no client; that resolves to the fallback profile, so
                # it keeps being served exactly what it was served before profiles existed.
                client = (params.get("client") or [""])[0].strip()
                try:
                    payload = service._lookup_via_main_thread(word, client)
                except TimeoutError as exc:
                    self._respond(503, {"error": str(exc)})
                except Exception:
                    logger.exception("word_lookup: lookup failed for %r", word)
                    self._respond(500, {"error": "lookup failed"})
                else:
                    self._respond(200, payload)

            def do_POST(self) -> None:
                # Nothing may escape, for the same reason do_GET catches broadly: socketserver
                # prints an unhandled handler exception to stderr, and Anki turns stderr into
                # its error dialog -- mid-review, from a request the user never saw.
                try:
                    self._serve_generate()
                except Exception:
                    logger.exception("word_lookup: generate request failed")
                    self._respond(500, {"error": "generate failed"})

            def _serve_generate(self) -> None:
                """Answer a POST: regenerate a note's fields, or check a phrase.

                One handler for both because everything up to the last step is identical — the
                same body limit, the same drain-before-deciding, the same refusal for anything a
                web page started. Splitting it would mean two copies of the guards, and a guard
                that exists twice is one that will eventually differ.
                """
                route = urlparse(self.path).path.rstrip("/")
                if route not in (_GENERATE_PATH, _CHECK_PATH):
                    self._respond(404, {"error": "unknown endpoint"})
                    return
                try:
                    # Read (and so drain) the body BEFORE deciding anything, so a refused
                    # client still gets its answer instead of a connection reset while it was
                    # mid-write -- and so a rejected request's leftover bytes can never be
                    # parsed as the next request should this handler ever speak HTTP/1.1
                    # (BaseHTTPRequestHandler defaults to 1.0, which closes after every reply).
                    # It is only PARSED once the guards below pass: an unauthenticated caller
                    # learns nothing about how its JSON was read.
                    raw = self._body_bytes()
                except _BadRequestError as exc:
                    self._respond(400, {"error": str(exc)})
                    return
                if is_page_origin(self.headers.get("Origin")):
                    # A page's fetch, not a clipper's. See the module docstring: no-cors still
                    # performs the side effect, and this side effect costs money.
                    # Named for BOTH routes rather than reworded for the newer one: /generate
                    # rewrites notes as well as spending credits, and a message that mentioned
                    # only the money would be a quiet downgrade of what it says about writes.
                    self._respond(
                        403,
                        {
                            "error": "requests from a web page are refused; "
                            "this endpoint spends your provider credits and "
                            "may change your notes"
                        },
                    )
                    return
                if route == _CHECK_PATH:
                    self._serve_check(raw)
                    return
                try:
                    payload = self._parse_generate(raw)
                except _BadRequestError as exc:
                    self._respond(400, {"error": str(exc)})
                    return
                client, note_id, fields = payload
                try:
                    result = service._regenerate(client, note_id, fields)
                except RegenerationDisabledError as exc:
                    self._respond(409, {"error": str(exc)})
                except (RegenerationUnavailableError, TimeoutError) as exc:
                    self._respond(503, {"error": str(exc)})
                except Exception:
                    logger.exception("word_lookup: generating note %s failed", note_id)
                    self._respond(500, {"error": "generate failed"})
                else:
                    self._respond(200, result)

            def _serve_check(self, raw: bytes) -> None:
                """Answer ``POST /check``: correct a phrase through phrase_check."""
                try:
                    body = _json_object(raw)
                except _BadRequestError as exc:
                    self._respond(400, {"error": str(exc)})
                    return
                text = str(body.get("text") or "").strip()
                if not text:
                    self._respond(400, {"error": "there is nothing to check"})
                    return
                if len(text) > MAX_PHRASE_CHARS:
                    self._respond(
                        400,
                        {
                            "error": (
                                f"That is too long to check at once — {len(text):,} "
                                f"characters, and the limit is {MAX_PHRASE_CHARS:,}. "
                                f"Select a sentence or two."
                            )
                        },
                    )
                    return
                try:
                    result = service._check_phrase(
                        text,
                        str(body.get("mode") or ""),
                        bool(body.get("refresh")),
                    )
                except PhraseCheckUnavailableError as exc:
                    # Phrase Check is off, or this build has none. A 503 the clipper renders as
                    # "switch it on", the same shape /generate uses for smart_notes.
                    self._respond(503, {"error": str(exc)})
                except CheckFailedError as exc:
                    # The model could not be reached, or answered something unreadable. Its own
                    # sentence, because it names the thing only the user can fix.
                    self._respond(502, {"error": str(exc)})
                except Exception:
                    logger.exception("word_lookup: checking a phrase failed")
                    self._respond(500, {"error": "the check failed"})
                else:
                    self._respond(200, result)

            def _body_bytes(self) -> bytes:
                """Return the request body, refusing one too big to buffer.

                Raises:
                    _BadRequestError: The length is unusable or over :data:`_MAX_BODY_BYTES`.
                """
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError as exc:
                    raise _BadRequestError("invalid Content-Length") from exc
                if length < 0:
                    raise _BadRequestError("invalid Content-Length")
                if length > _MAX_BODY_BYTES:
                    # Refused unread, so what is left on the socket is not a request: this
                    # connection cannot be reused.
                    self.close_connection = True
                    raise _BadRequestError("request body too large")
                return self.rfile.read(length) if length else b""

            @staticmethod
            def _parse_generate(raw: bytes) -> tuple[str, int, Optional[list[str]]]:
                """Validate the request body into ``(client, note_id, fields)``.

                Raises:
                    _BadRequestError: Anything about the body this service will not act on.
                """
                body = _json_object(raw)
                note_id = body.get("note_id")
                # ``bool`` is an ``int`` in Python, and ``True`` is not a note id.
                if isinstance(note_id, bool) or not isinstance(note_id, (int, str)):
                    raise _BadRequestError("missing 'note_id'")
                try:
                    parsed_id = int(note_id)
                except ValueError as exc:
                    raise _BadRequestError("missing 'note_id'") from exc
                fields = body.get("fields")
                if fields is not None and (
                    not isinstance(fields, list)
                    or not all(isinstance(name, str) for name in fields)
                ):
                    raise _BadRequestError(
                        "'fields' must be a list of field names or null"
                    )
                client = body.get("client")
                return (
                    str(client or "").strip(),
                    parsed_id,
                    list(fields) if fields is not None else None,
                )

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
