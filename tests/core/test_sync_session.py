"""Tests for one machine serving another: the session, the client, and every way it fails.

These run a REAL session on loopback and a real client against it. The transport is the part
that cannot be reasoned about from the types — a header name, a status code, a socket that was
never closed — and a fake on both sides would agree with itself about all three.

What is pinned here, beyond the happy path, is ADR-020's conditions and the client's one rule:
every failure gets a sentence naming what to do. "Could not connect" is the answer that leaves
a user with nothing to try, and four different conditions produce it if nobody separates them.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnia.core.sync import (
    HELLO_PATH,
    DeckEntry,
    Inventory,
    NoteTypeEntry,
    PairingAddress,
    Session,
    SyncClient,
    SyncError,
    check,
    new_token,
)


def _inventory() -> Inventory:
    return Inventory(
        machine="the-mac",
        omnia_version="v0.1.0",
        decks=(
            DeckEntry(1, "English", 0),
            DeckEntry(2, "English::Verbs", 1200),
        ),
        note_types=(NoteTypeEntry("AnkiVocabulary", ("Word", "Sentence"), 1200),),
    )


@pytest.fixture
def served():
    """A session on loopback and a client holding its key — torn down either way."""
    key = new_token()
    session = Session(_inventory, key=key, host="127.0.0.1", port=0)
    assert session.start(), "the session did not open a socket"
    try:
        yield session, SyncClient(
            PairingAddress("127.0.0.1", session.port, key), timeout=5
        )
    finally:
        session.stop()


class TestOneMachineAsksAnother:
    def test_hello_says_it_is_there_and_speaks_the_same_protocol(self, served):
        _session, client = served

        answer = client.hello()

        assert answer["ok"] is True
        assert answer["protocol"] == 1

    def test_the_inventory_arrives_whole(self, served):
        _session, client = served

        inventory = client.inventory()

        assert inventory.machine == "the-mac"
        assert [deck.name for deck in inventory.decks] == ["English", "English::Verbs"]
        assert inventory.decks[1].cards == 1200
        assert inventory.note_types[0].fields == ("Word", "Sentence")

    def test_the_deck_tree_keeps_its_shape(self, served):
        _session, client = served

        tree = client.inventory().deck_tree()

        assert [(deck.name, depth) for deck, depth in tree] == [
            ("English", 0),
            ("English::Verbs", 1),
        ]

    def test_check_says_nothing_when_it_works(self, served):
        session, _client = served

        assert (
            check(PairingAddress("127.0.0.1", session.port, session._key), timeout=5)
            == ""
        )


class TestTheKeyIsTheKey:
    def test_a_wrong_key_is_refused(self, served):
        session, _client = served
        wrong = SyncClient(
            PairingAddress("127.0.0.1", session.port, new_token()), timeout=5
        )

        with pytest.raises(SyncError, match="does not open"):
            wrong.hello()

    def test_no_key_at_all_gets_the_same_answer_as_a_wrong_one(self, served):
        # Telling the two apart tells a caller which half they got right.
        session, _client = served
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{session.port}{HELLO_PATH}", timeout=5
            )

        assert caught.value.code == 403

    def test_the_collection_is_never_read_for_an_unauthorised_request(self):
        # The refusal happens BEFORE the inventory callable is reached, so a stranger cannot make
        # this machine touch its collection at all.
        reads = []

        def _count() -> Inventory:
            reads.append(1)
            return Inventory()

        session = Session(_count, key=new_token(), host="127.0.0.1", port=0)
        assert session.start()
        try:
            wrong = SyncClient(
                PairingAddress("127.0.0.1", session.port, new_token()), timeout=5
            )
            with pytest.raises(SyncError):
                wrong.inventory()
        finally:
            session.stop()

        assert reads == []

    def test_a_session_without_a_key_refuses_to_open(self):
        # Serving with no key is serving to anyone who can reach the port.
        session = Session(_inventory, key="", host="127.0.0.1", port=0)

        assert session.start() is False
        assert session.running is False


class TestTheSocketsLifecycle:
    def test_starting_twice_is_idempotent(self, served):
        session, _client = served

        assert session.start() is True
        assert session.running is True

    def test_stopping_something_that_never_started_is_safe(self):
        Session(_inventory, key=new_token(), host="127.0.0.1", port=0).stop()

    def test_the_port_reported_is_the_one_actually_bound(self, served):
        session, _client = served

        assert session.port != 0
        # And it is really listening there — the ID would carry this number.
        with socket.create_connection(("127.0.0.1", session.port), timeout=5):
            pass

    def test_stop_releases_the_port(self):
        session = Session(_inventory, key=new_token(), host="127.0.0.1", port=0)
        session.start()
        port = session.port

        session.stop()

        assert session.running is False
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2)


class TestEveryFailureNamesItsFix:
    def test_nothing_listening_says_sharing_is_off(self):
        # The commonest state while setting this up, and the one a generic "could not connect"
        # leaves the user guessing about.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]

        message = check(PairingAddress("127.0.0.1", dead_port, new_token()), timeout=5)

        assert "turn sharing on" in message

    def test_a_busy_collection_is_reported_as_busy_not_as_broken(self):
        def _busy() -> Inventory:
            raise TimeoutError("the main thread never came free")

        session = Session(_busy, key=new_token(), host="127.0.0.1", port=0)
        assert session.start()
        try:
            client = SyncClient(
                PairingAddress("127.0.0.1", session.port, session._key), timeout=5
            )
            with pytest.raises(SyncError, match="busy"):
                client.inventory()
        finally:
            session.stop()

    def test_a_broken_source_does_not_take_the_session_down_with_it(self):
        # One failed read must not end the sharing session: the next request still gets served.
        state = {"fail": True}

        def _flaky() -> Inventory:
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("the collection is not there")
            return _inventory()

        session = Session(_flaky, key=new_token(), host="127.0.0.1", port=0)
        assert session.start()
        try:
            client = SyncClient(
                PairingAddress("127.0.0.1", session.port, session._key), timeout=5
            )
            with pytest.raises(SyncError):
                client.inventory()

            assert client.inventory().machine == "the-mac"
        finally:
            session.stop()

    def test_an_unknown_endpoint_says_the_builds_differ(self, served):
        session, _client = served
        client = SyncClient(
            PairingAddress("127.0.0.1", session.port, session._key), timeout=5
        )

        with pytest.raises(SyncError, match="up to date"):
            client._get("/sync/something-newer")

    @pytest.mark.parametrize(
        "protocol,expected",
        [(99, "update this machine"), (0, "update that machine")],
    )
    def test_a_protocol_mismatch_says_which_machine_to_update(self, protocol, expected):
        # Refused by NUMBER rather than by whatever field happens to be missing, so the message
        # can name the machine instead of surfacing a KeyError from three layers down.
        key = new_token()
        body = json.dumps(
            {"protocol": protocol, "decks": [], "note_types": []}
        ).encode()

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = SyncClient(
                PairingAddress("127.0.0.1", server.server_address[1], key), timeout=5
            )
            with pytest.raises(SyncError, match=expected):
                client.inventory()
        finally:
            server.shutdown()
            server.server_close()

    def test_an_answer_that_is_not_an_inventory_says_so(self):
        key = new_token()

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"<html>a login page</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = SyncClient(
                PairingAddress("127.0.0.1", server.server_address[1], key), timeout=5
            )
            with pytest.raises(SyncError, match="did not answer with an inventory"):
                client.inventory()
        finally:
            server.shutdown()
            server.server_close()
