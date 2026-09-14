"""Tests for one machine serving another: the session, the client, and every way it fails.

These run a REAL session on loopback and a real client against it. The transport is the part
that cannot be reasoned about from the types — a header name, a status code, a socket that was
never closed — and a fake on both sides would agree with itself about all three.

What is pinned here, beyond the happy path, is ADR-020's conditions and the client's one rule:
every failure gets a sentence naming what to do. "Could not connect" is the answer that leaves
a user with nothing to try, and four different conditions produce it if nobody separates them.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnia.core.sync import (
    HELLO_PATH,
    MAX_ATTEMPTS,
    PROTOCOL,
    TOKEN_HEADER,
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


@contextlib.contextmanager
def _peer(answer: bytes, *, status: int = 200, content_type: str = "application/json"):
    """Something OTHER than Omnia on the port an ID names, torn down either way.

    An ID carries a port, and a port on another machine can be held by anything — a router page,
    a dev server, an ssh daemon, another add-on. Every one of those is an ordinary setup mistake
    rather than a bug, so each has to come back as a sentence; this is the stand-in they all use.

    Yields:
        The :class:`PairingAddress` pointing at it. The token is a fresh one nobody honours,
        which is the point: these peers do not check it.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield PairingAddress("127.0.0.1", server.server_address[1], new_token())
    finally:
        server.shutdown()
        server.server_close()


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
        assert answer["protocol"] == PROTOCOL

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

    def test_a_key_with_a_byte_no_ascii_can_hold_is_refused_like_any_other(
        self, served
    ):
        # HTTP headers decode as latin-1, so a peer can put any byte in this one. Compared as
        # str, compare_digest RAISES on non-ASCII — and then the request got no answer at all
        # (so "no key" and "wrong key" stopped being indistinguishable), the refusal was never
        # logged, and the traceback went to stderr, which inside Anki pops its error dialog.
        import http.client

        session, _client = served
        connection = http.client.HTTPConnection("127.0.0.1", session.port, timeout=5)
        try:
            connection.request("GET", HELLO_PATH, headers={TOKEN_HEADER: "kéy-with-é"})
            response = connection.getresponse()

            assert response.status == 403
            assert b"does not open" in response.read()
        finally:
            connection.close()

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


class TestASilentPeerCannotPinAThread:
    def test_a_connection_that_says_nothing_is_dropped(self, served):
        # Without a handler timeout this parks a thread inside readline() for ever, and stop()
        # cannot reclaim it: it closes the listening socket and joins serve_forever while the
        # parked handler outlives both — and the switch the user turned off.
        session, client = served
        silent = socket.create_connection(("127.0.0.1", session.port), timeout=5)
        try:
            # The session is still serving everyone else while that one sits there.
            assert client.hello()["ok"] is True
            assert session._handler().timeout == 5, "the handler has no deadline"
        finally:
            silent.close()


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
        body = json.dumps(
            {"protocol": protocol, "decks": [], "note_types": []}
        ).encode()

        with _peer(body) as address:
            with pytest.raises(SyncError, match=expected):
                SyncClient(address, timeout=5).inventory()

    def test_check_returns_a_sentence_when_something_else_holds_the_port(self):
        # check() promises the call site needs no exception handling, so a 200 full of HTML has
        # to come back as words rather than as a JSONDecodeError.
        with _peer(b"<html>router login</html>", content_type="text/html") as address:
            assert "was not Omnia" in check(address, timeout=5)

    def test_something_answering_json_that_is_not_omnia_is_not_called_an_old_omnia(
        self,
    ):
        # The trap this closes: a missing `protocol` read as 0, which is < PROTOCOL, so a dev
        # server answering {"status": "ok"} told the user "that machine runs an older Omnia —
        # update it". They would then go and update software that was never the problem, which
        # is the one thing this module promises not to do. A body with no protocol is not an old
        # Omnia; it is not an Omnia.
        with _peer(b'{"status": "ok"}') as address:
            message = check(address, timeout=5)

        assert "was not Omnia" in message
        assert "older Omnia" not in message

    @staticmethod
    def _raw_listener(reply: bytes):
        """A bare socket on loopback that answers ``reply`` and closes. Returns the port."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def _serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.recv(4096)
                    if reply:
                        conn.sendall(reply)
            except OSError:
                pass
            finally:
                listener.close()

        threading.Thread(target=_serve, daemon=True).start()
        return listener.getsockname()[1]

    def test_a_port_held_by_something_that_is_not_http_returns_a_sentence(self):
        # urllib wraps only what h.request() raises; getresponse() is outside that guard, so a
        # non-HTTP banner came out as a bare BadStatusLine — and inside Anki an exception
        # escaping a QueryOp callback pops the error dialog.
        port = self._raw_listener(b"SSH-2.0-OpenSSH_9.6\r\n")

        message = check(PairingAddress("127.0.0.1", port, new_token()), timeout=5)

        assert "was not Omnia" in message

    def test_a_port_that_accepts_and_closes_returns_a_sentence(self):
        # A service still starting, or a firewall that resets rather than refuses.
        port = self._raw_listener(b"")

        message = check(PairingAddress("127.0.0.1", port, new_token()), timeout=5)

        assert "was not Omnia" in message

    def test_check_says_which_machine_to_update_on_a_protocol_mismatch(self):
        # On the CHECK, not later: "it works" followed by a failure once the user has gone on to
        # choose what to copy is the answer arriving in the wrong place.
        body = json.dumps({"ok": True, "protocol": 99}).encode()

        with _peer(body) as address:
            assert "update this machine" in check(address, timeout=5)

    def test_an_answer_that_is_not_an_inventory_says_so(self):
        with _peer(b"<html>a login page</html>", content_type="text/html") as address:
            with pytest.raises(SyncError, match="did not answer with an inventory"):
                SyncClient(address, timeout=5).inventory()

    def test_a_refusal_says_what_to_do_about_it_not_only_what_happened(self):
        # The service cannot say why: it must answer the same for a missing code and a wrong
        # one, or the difference tells a caller which half they got right. THIS side knows there
        # is a fix — the code was regenerated over there — so the sentence carries it.
        with _peer(
            b'{"error": "that code does not open this machine"}', status=403
        ) as address:
            message = check(address, timeout=5)

        assert "read it off that screen again" in message

    def test_a_locked_out_machine_says_to_wait_rather_than_to_retype(self):
        # Driven against the REAL session rather than a stub, because the sentence and the
        # lockout that produces it have to agree: a 429 means the code may well be right, and
        # telling the user to check it would send them to re-read a number that was fine.
        key = new_token()
        session = Session(_inventory, key=key, host="127.0.0.1", port=0)
        assert session.start()
        try:
            wrong = PairingAddress("127.0.0.1", session.port, "0" * len(key))
            for _ in range(MAX_ATTEMPTS):
                check(wrong, timeout=5)

            message = check(wrong, timeout=5)
        finally:
            session.stop()

        assert "wait" in message.lower()


class TestTheCodeCannotBeGuessedAt:
    """The half of the design that makes a nine-digit code safe.

    Nine digits is a billion combinations — an afternoon's work for a script that can try them
    as fast as the network allows, and centuries for one allowed five tries a minute. The short
    code and this are one decision; reviewing either alone gets the wrong answer.
    """

    def _lockout(self):
        from omnia.core.sync.service import _Lockout

        self.now = 0.0
        return _Lockout(limit=3, seconds=60.0, clock=lambda: self.now)

    def test_a_few_wrong_codes_are_tolerated(self):
        # Somebody copying nine digits off another screen WILL get one wrong.
        lockout = self._lockout()

        assert lockout.record_failure() is False
        assert lockout.record_failure() is False
        assert lockout.locked is False

    def test_too_many_closes_the_door(self):
        lockout = self._lockout()
        for _ in range(3):
            lockout.record_failure()

        assert lockout.locked is True

    def test_the_door_opens_again_after_the_wait(self):
        # A lockout that never lifted would be a way for anyone reachable to disable the feature
        # permanently; it has to be a delay, not a kill switch.
        lockout = self._lockout()
        for _ in range(3):
            lockout.record_failure()

        self.now += 61.0

        assert lockout.locked is False

    def test_it_closes_again_on_the_next_wrong_code_after_it_lifts(self):
        # The counter has to RESET when a lockout expires, or the very next wrong code trips it
        # again instantly and the delay compounds without anyone guessing.
        lockout = self._lockout()
        for _ in range(3):
            lockout.record_failure()
        self.now += 61.0

        assert lockout.record_failure() is False
        assert lockout.locked is False

    def test_the_right_code_forgets_the_wrong_ones(self):
        lockout = self._lockout()
        lockout.record_failure()
        lockout.record_failure()

        lockout.record_success()

        assert lockout.record_failure() is False
        assert lockout.locked is False

    def test_the_collection_is_never_read_while_locked_out(self):
        # The refusal happens BEFORE the code is compared and before anything touches Anki, so
        # a machine under a guessing run is not also a machine doing work for it.
        reads = []

        def _read():
            reads.append(1)
            return _inventory()

        key = new_token()
        session = Session(_read, key=key, host="127.0.0.1", port=0)
        assert session.start()
        try:
            wrong = PairingAddress("127.0.0.1", session.port, "0" * len(key))
            for _ in range(MAX_ATTEMPTS + 3):
                check(wrong, timeout=5)
        finally:
            session.stop()

        assert reads == []

    def test_the_right_code_still_works_after_a_single_slip(self):
        key = new_token()
        session = Session(_inventory, key=key, host="127.0.0.1", port=0)
        assert session.start()
        try:
            check(PairingAddress("127.0.0.1", session.port, "0" * len(key)), timeout=5)

            assert (
                check(PairingAddress("127.0.0.1", session.port, key), timeout=5) == ""
            )
        finally:
            session.stop()
