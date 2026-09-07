"""Tests for ``POST /generate`` — the first endpoint on this service that WRITES.

The reads (``/lookup``, ``/media``) are open by design: they can only tell a caller what is
already in the collection. This one overwrites note fields and spends the user's LLM/TTS
credits, so most of what follows is about what it REFUSES: a request with no token, a request
with the wrong token, and a request a web page started (an ``Origin`` header), whose side effect
would happen even though the page could never read the answer.

The generate callable is injected, so nothing here needs smart_notes, Anki or a network.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from omnia.plugins.word_lookup import service as service_module
from omnia.plugins.word_lookup.service import (
    LookupService,
    RegenerationDisabledError,
    RegenerationUnavailableError,
)

_TOKEN = "a-perfectly-good-token"


def _free_port() -> int:
    """Return a port that is free right now (bind to 0 and read what the OS gave us)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _post(port: int, path: str, body=None, **headers) -> tuple[int, dict]:
    """POST ``body`` as JSON and return ``(status, parsed_json)``."""
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST"
    )
    request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        if value is not None:
            request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _raw_request(port: int, request: bytes) -> str:
    """Send ``request`` verbatim and return the whole response (headers included).

    Read to EOF rather than once: the handler speaks HTTP/1.0, so it closes the connection
    after answering, and a single ``recv`` can return the headers before the body has arrived.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return b"".join(chunks).decode("latin-1")
            chunks.append(chunk)


@pytest.fixture
def serve():
    """Start a service with an injected generate callable; always stop it."""
    started: list[LookupService] = []

    def make(generate=None, *, token: str = _TOKEN, lookup=None) -> int:
        service = LookupService(
            lookup or (lambda word, client: {"word": word, "client": client}),
            generate=generate,
            token=token,
            port=_free_port(),
        )
        assert service.start(), "the service did not bind"
        started.append(service)
        return service._port

    yield make
    for service in started:
        service.stop()


def _echo(client, note_id, fields):
    """A generate callable that reports what it was asked for."""
    return {"note_id": note_id, "client": client, "fields": fields, "results": []}


class TestAGoodRequest:
    def test_it_answers_200_with_the_payload(self, serve):
        port = serve(
            lambda client, note_id, fields: {"note_id": note_id, "results": []}
        )

        status, body = _post(port, "/generate", {"note_id": 7}, X_Omnia_Token=_TOKEN)

        assert status == 200
        assert body == {"note_id": 7, "results": []}

    def test_the_client_note_and_fields_reach_the_callable(self, serve):
        port = serve(_echo)

        _status, body = _post(
            port,
            "/generate",
            {"client": "web_clipper", "note_id": 12, "fields": ["A", "B"]},
            X_Omnia_Token=_TOKEN,
        )

        assert (body["client"], body["note_id"], body["fields"]) == (
            "web_clipper",
            12,
            ["A", "B"],
        )

    @pytest.mark.parametrize("body", [{"note_id": 5}, {"note_id": 5, "fields": None}])
    def test_no_fields_means_every_field(self, serve, body):
        port = serve(_echo)

        _status, answer = _post(port, "/generate", body, X_Omnia_Token=_TOKEN)

        assert answer["fields"] is None

    def test_a_note_id_sent_as_a_string_is_accepted(self, serve):
        """A JS client that stringifies ids must not be told its note id is missing."""
        port = serve(_echo)

        _status, body = _post(
            port, "/generate", {"note_id": "42"}, X_Omnia_Token=_TOKEN
        )

        assert body["note_id"] == 42


class TestTheToken:
    """The token is what separates the user's clipper from anything else on the machine."""

    def test_no_token_is_a_401(self, serve):
        calls: list = []
        port = serve(lambda *args: calls.append(args) or {})

        status, body = _post(port, "/generate", {"note_id": 1})

        assert status == 401 and "token" in body["error"].lower()
        assert calls == [], "an unauthenticated request reached the generator"

    def test_a_wrong_token_of_the_same_length_is_a_401(self, serve):
        port = serve(_echo)

        wrong = "b" + _TOKEN[1:]
        status, _body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=wrong)

        assert status == 401

    def test_no_configured_token_keeps_the_write_path_shut(self, serve):
        """Failing to issue a token must never be the same as having no lock at all."""
        port = serve(_echo, token="")

        assert _post(port, "/generate", {"note_id": 1})[0] == 401
        assert _post(port, "/generate", {"note_id": 1}, X_Omnia_Token="")[0] == 401

    def test_it_is_compared_in_constant_time(self, serve, monkeypatch):
        """A naive ``==`` leaks the secret one byte at a time to a caller that can time us."""
        seen: list[tuple[bytes, bytes]] = []
        real = service_module.hmac.compare_digest

        def spy(left, right):
            seen.append((left, right))
            return real(left, right)

        monkeypatch.setattr(service_module.hmac, "compare_digest", spy)
        port = serve(_echo)

        assert _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)[0] == 200
        assert seen == [(_TOKEN.encode(), _TOKEN.encode())]

    def test_a_non_ascii_token_is_refused_not_a_crash(self, serve):
        """``compare_digest`` rejects a non-ASCII ``str`` outright; the header is untrusted."""
        port = serve(_echo)

        status, _body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token="tökén")

        assert status == 401


class TestARequestAPageStarted:
    """No CORS headers stop a side effect: ``fetch(…, {mode: "no-cors"})`` still runs it.

    The rule reads the ``Origin`` VALUE. Refusing the header's mere presence would refuse the
    web clipper too: the Fetch spec appends ``Origin`` to every non-GET/HEAD request, so the
    extension's own POST carries one. A page cannot fake an extension origin — ``Origin`` is a
    forbidden header name, filled in by the browser.
    """

    @pytest.mark.parametrize(
        "origin",
        ["https://evil.example", "http://evil.example", "http://127.0.0.1"],
    )
    def test_a_web_origin_is_a_403(self, serve, origin: str):
        """``http://127.0.0.1`` included: a page served from localhost is still a page."""
        calls: list = []
        port = serve(lambda *args: calls.append(args) or {})

        status, body = _post(
            port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN, Origin=origin
        )

        assert status == 403 and "web page" in body["error"]
        assert calls == [], "a page's request reached the generator"

    def test_it_outranks_the_token_check(self, serve):
        """Even a request with a valid-looking token is refused when a page started it."""
        port = serve(_echo)

        status, _body = _post(
            port,
            "/generate",
            {"note_id": 1},
            X_Omnia_Token="wrong",
            Origin="https://evil.example",
        )

        assert status == 403

    def test_the_extensions_own_origin_is_allowed(self, serve):
        """The web clipper's service worker sends this, and the id is not ours to pin."""
        port = serve(_echo)

        status, _body = _post(
            port,
            "/generate",
            {"note_id": 1},
            X_Omnia_Token=_TOKEN,
            Origin="chrome-extension://abc",
        )

        assert status == 200

    def test_no_origin_at_all_is_allowed(self, serve):
        """What a native client sends — the desktop clipper's ``urllib`` request."""
        port = serve(_echo)

        assert _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)[0] == 200

    def test_an_extension_origin_still_needs_the_token(self, serve):
        """The origin check is the second layer; the token is the authentication."""
        port = serve(_echo)

        status, _body = _post(
            port,
            "/generate",
            {"note_id": 1},
            X_Omnia_Token="wrong",
            Origin="chrome-extension://abc",
        )

        assert status == 401


class TestReadingAnOriginValue:
    """The predicate behind the 403, tested directly for the values a socket test cannot send."""

    @pytest.mark.parametrize("origin", [None, "", "chrome-extension://abc"])
    def test_a_client_origin_is_not_a_page(self, origin):
        assert service_module.is_page_origin(origin) is False

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.example",
            "http://127.0.0.1:8766",
            "null",  # a sandboxed iframe: still a page, and one hiding its own name
            "file://",
        ],
    )
    def test_anything_else_is_a_page(self, origin):
        assert service_module.is_page_origin(origin) is True

    def test_the_scheme_is_matched_case_insensitively(self):
        assert service_module.is_page_origin("Chrome-Extension://ABC") is False


class TestABodyThisServiceWillNotActOn:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"note_id": None},
            {"note_id": True},
            {"note_id": "not-a-number"},
            {"client": "web_clipper"},
        ],
    )
    def test_a_missing_or_unusable_note_id_is_a_400(self, serve, body):
        port = serve(_echo)

        status, answer = _post(port, "/generate", body, X_Omnia_Token=_TOKEN)

        assert status == 400 and "note_id" in answer["error"]

    @pytest.mark.parametrize("fields", ["Definition", 5, ["A", 2], {"A": 1}])
    def test_fields_must_be_a_list_of_names(self, serve, fields):
        port = serve(_echo)

        status, answer = _post(
            port, "/generate", {"note_id": 1, "fields": fields}, X_Omnia_Token=_TOKEN
        )

        assert status == 400 and "fields" in answer["error"]

    def test_a_body_that_is_not_json_is_a_400(self, serve):
        port = serve(_echo)

        response = _raw_request(
            port,
            b"POST /generate HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"X-Omnia-Token: " + _TOKEN.encode() + b"\r\n"
            b"Content-Length: 5\r\n\r\nnope!",
        )

        assert "400" in response.splitlines()[0]

    def test_a_json_array_is_a_400(self, serve):
        port = serve(_echo)

        status, _answer = _post(port, "/generate", [1, 2], X_Omnia_Token=_TOKEN)

        assert status == 400

    def test_a_body_too_big_to_buffer_is_refused_unread(self, serve):
        """The cap is what an unauthenticated caller may make Anki allocate."""
        port = serve(_echo)

        response = _raw_request(
            port,
            b"POST /generate HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: 99999999\r\n\r\n",
        )

        assert "400" in response.splitlines()[0]
        assert "too large" in response

    def test_a_nonsense_content_length_is_a_400(self, serve):
        port = serve(_echo)

        response = _raw_request(
            port,
            b"POST /generate HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: abc\r\n\r\n",
        )

        assert "400" in response.splitlines()[0]


class TestWhenGenerationCannotHappen:
    def test_the_option_being_off_is_a_409_that_names_it(self, serve):
        """The clipper shows this message verbatim, so it has to say what to switch on."""

        def refuse(client, note_id, fields):
            raise RegenerationDisabledError(
                "Regenerating from a clipper is off — turn on "
                "“Regenerate from clippers” in Smart Notes → General."
            )

        port = serve(refuse)

        status, body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 409
        assert "Regenerate from clippers" in body["error"]

    def test_smart_notes_being_off_is_a_503(self, serve):
        def unavailable(client, note_id, fields):
            raise RegenerationUnavailableError("Smart Notes is off")

        port = serve(unavailable)

        status, body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 503 and "Smart Notes" in body["error"]

    def test_a_service_with_no_generator_is_a_503(self, serve):
        port = serve(None)

        status, _body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 503

    def test_a_wedged_main_thread_is_a_503(self, serve):
        def timeout(client, note_id, fields):
            raise TimeoutError("Anki's main thread did not answer in time")

        port = serve(timeout)

        status, _body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 503

    def test_anything_else_is_a_500_that_leaks_nothing(self, serve):
        def boom(client, note_id, fields):
            raise RuntimeError("secret internal detail")

        port = serve(boom)

        status, body = _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 500
        assert "secret internal detail" not in json.dumps(body)


class TestRoutingAndIsolation:
    def test_posting_to_another_path_is_a_404(self, serve):
        port = serve(_echo)

        status, _body = _post(port, "/lookup", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert status == 404

    def test_lookup_still_needs_no_token(self, serve):
        """Turning the write path on may not put a lock on the reads."""
        port = serve(_echo)

        status, body = _get(port, "/lookup?word=plunge")

        assert status == 200 and body["word"] == "plunge"

    def test_a_slow_generation_does_not_block_a_lookup(self, serve):
        """The server threads each connection, so an LLM call cannot wedge the magnifier."""
        started, release = threading.Event(), threading.Event()

        def slow(client, note_id, fields):
            started.set()
            release.wait(10)
            return {"note_id": note_id, "results": []}

        port = serve(slow)
        answer: list[tuple[int, dict]] = []
        caller = threading.Thread(
            target=lambda: answer.append(
                _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)
            )
        )
        caller.start()
        try:
            assert started.wait(5), "the generate request never reached the callable"
            assert _get(port, "/lookup?word=plunge")[0] == 200
        finally:
            release.set()
            caller.join(10)
        assert answer and answer[0][0] == 200

    def test_a_failure_never_reaches_stderr(self, serve, capfd):
        """socketserver prints an escaped exception to stderr, and Anki pops a DIALOG on that."""

        def boom(client, note_id, fields):
            raise RuntimeError("kaboom")

        port = serve(boom)

        assert _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)[0] == 500
        assert "Traceback" not in capfd.readouterr().err

    def test_the_service_keeps_serving_afterwards(self, serve):
        def boom(client, note_id, fields):
            raise RuntimeError("kaboom")

        port = serve(boom)
        _post(port, "/generate", {"note_id": 1}, X_Omnia_Token=_TOKEN)

        assert _get(port, "/lookup?word=after")[0] == 200
