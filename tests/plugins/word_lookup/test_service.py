"""Tests for the loopback lookup service (real sockets, injected lookup — no Anki needed)."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from omnia.plugins.word_lookup.service import LookupService


def _free_port() -> int:
    """Return a port that is free right now (bind to 0 and read what the OS gave us)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, path: str) -> tuple[int, dict]:
    """GET ``path`` and return ``(status, parsed_json)``, treating HTTP errors as responses."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture
def service_factory():
    """Start services and guarantee they are stopped, so no test leaks a bound port."""
    started: list[LookupService] = []

    def make(lookup, **kwargs) -> tuple[LookupService, int]:
        port = kwargs.pop("port", None) or _free_port()
        service = LookupService(lookup, port=port, **kwargs)
        assert service.start() is True
        started.append(service)
        return service, port

    yield make
    for service in started:
        service.stop()


class TestLookupEndpoint:
    def test_returns_the_lookup_payload(self, service_factory):
        _service, port = service_factory(
            lambda word: {"word": word, "cards": [{"title": word}]}
        )
        status, body = _get(port, "/lookup?word=plunge")
        assert status == 200
        assert body == {"word": "plunge", "cards": [{"title": "plunge"}]}

    def test_url_encoded_and_unicode_words_survive(self, service_factory):
        _service, port = service_factory(lambda word: {"echo": word})
        status, body = _get(port, "/lookup?word=lao%20xu%E1%BB%91ng")
        assert status == 200 and body == {"echo": "lao xuống"}

    def test_missing_word_is_a_400(self, service_factory):
        _service, port = service_factory(lambda word: {"never": "called"})
        status, body = _get(port, "/lookup")
        assert status == 400 and "word" in body["error"]

    def test_blank_word_is_a_400(self, service_factory):
        _service, port = service_factory(lambda word: {"never": "called"})
        status, _body = _get(port, "/lookup?word=%20%20")
        assert status == 400

    def test_unknown_path_is_a_404(self, service_factory):
        _service, port = service_factory(lambda word: {})
        status, _body = _get(port, "/something-else")
        assert status == 404

    def test_lookup_failure_is_a_500_without_leaking_details(self, service_factory):
        def boom(_word):
            raise RuntimeError("secret internal detail")

        _service, port = service_factory(boom)
        status, body = _get(port, "/lookup?word=x")
        assert status == 500
        assert "secret internal detail" not in json.dumps(body)


class TestAClientThatHangsUp:
    """A disconnect mid-response must not reach stderr — Anki turns stderr into its ERROR
    DIALOG, so a clipper the user simply closes would interrupt their review."""

    def test_disconnect_mid_response_is_not_reported_to_stderr(
        self, service_factory, capfd
    ):
        ready = threading.Event()

        def slow_lookup(word: str) -> dict:
            ready.set()
            # Long enough that the client below is gone before the write starts.
            threading.Event().wait(0.6)
            return {"word": word, "cards": [{"title": "x" * 5000}]}

        _service, port = service_factory(slow_lookup)

        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(
            b"GET /lookup?word=gone HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n"
        )
        assert ready.wait(5), "the handler never reached the lookup"
        sock.close()  # hang up while the server is still building the answer

        # The server thread needs a moment to finish and (previously) blow up.
        threading.Event().wait(1.5)
        assert "Traceback" not in capfd.readouterr().err

    def test_the_service_still_serves_the_next_request(self, service_factory):
        _service, port = service_factory(lambda word: {"word": word})

        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET /lookup?word=first HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        sock.close()

        status, body = _get(port, "/lookup?word=second")
        assert (status, body) == (200, {"word": "second"})


class TestMainThreadMarshalling:
    """Collection reads must happen on Qt's main thread, not the HTTP worker thread."""

    def test_lookup_runs_through_run_on_main(self, service_factory):
        ran_on: list[str] = []

        def run_on_main(work):
            # Stand in for Anki's taskman: run the work on a *different*, designated thread.
            thread = threading.Thread(target=work, name="fake-main")
            thread.start()
            thread.join()

        def lookup(word):
            ran_on.append(threading.current_thread().name)
            return {"word": word}

        _service, port = service_factory(lookup, run_on_main=run_on_main)
        status, _body = _get(port, "/lookup?word=x")
        assert status == 200
        assert ran_on == ["fake-main"]  # not the HTTP worker thread

    def test_a_wedged_main_thread_becomes_503_not_a_hang(
        self, service_factory, monkeypatch
    ):
        monkeypatch.setattr(
            "omnia.plugins.word_lookup.service._MAIN_THREAD_TIMEOUT_SECONDS", 0.2
        )
        _service, port = service_factory(
            lambda word: {"unreachable": True},
            run_on_main=lambda work: None,  # never runs the work: Anki busy/frozen
        )
        status, body = _get(port, "/lookup?word=x")
        assert status == 503 and "time" in body["error"].lower()

    def test_exception_on_the_main_thread_propagates_as_500(self, service_factory):
        def run_on_main(work):
            work()

        def boom(_word):
            raise ValueError("bad note")

        _service, port = service_factory(boom, run_on_main=run_on_main)
        status, _body = _get(port, "/lookup?word=x")
        assert status == 500


class TestLifecycle:
    def test_start_is_idempotent_and_stop_releases_the_port(self):
        port = _free_port()
        service = LookupService(lambda word: {}, port=port)
        assert service.start() is True
        assert service.start() is True  # already running
        assert service.running is True
        service.stop()
        assert service.running is False
        # The port is free again: a second service can bind it.
        again = LookupService(lambda word: {}, port=port)
        assert again.start() is True
        again.stop()

    def test_stop_without_start_is_safe(self):
        LookupService(lambda word: {}).stop()

    def test_bind_failure_reports_false_instead_of_raising(self):
        port = _free_port()
        first = LookupService(lambda word: {}, port=port)
        assert first.start() is True
        try:
            second = LookupService(lambda word: {}, port=port)
            assert second.start() is False  # port taken -> reported, not raised
            assert second.running is False
        finally:
            first.stop()

    def test_refuses_to_bind_a_non_loopback_host(self):
        # A lookup endpoint exposed off-machine would leak the user's collection.
        service = LookupService(lambda word: {}, host="0.0.0.0", port=_free_port())
        assert service.start() is False
        assert service.running is False
