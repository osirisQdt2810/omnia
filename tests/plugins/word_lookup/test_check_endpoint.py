"""Tests for ``POST /check``: correcting a phrase a clipper sent.

It shares a socket and every guard with ``/generate``, which is the point — one service for the
clippers to be told about, and one place the refusals live. So most of this is about the two
things that differ: what it does with the body, and how it tells "Phrase Check is off" apart from
"the check was tried and failed".

A real server on loopback, driven over a real socket, because the routing and the body limit are
what this adds and neither can be seen from a fake.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from omnia.plugins.word_lookup.service import (
    CheckFailedError,
    LookupService,
    PhraseCheckUnavailableError,
)

CORRECTION = {
    "original": "I have went.",
    "rewritten": "I have gone.",
    "mode": "written",
    "fixes": [{"before": "have went", "after": "have gone", "why": "tense"}],
    "highlight": [["I have ", False], ["gone.", True]],
}


def _post(port: int, path: str, body, **headers):
    """One POST, returning ``(status, decoded body)``."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else b"",
        method="POST",
        headers={
            "Content-Type": "application/json",
            **{name.replace("_", "-"): value for name, value in headers.items()},
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _free_port() -> int:
    """A port nothing holds right now. The service binds an explicit one, so it is asked for."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _lookup(word, client):
    return {"word": word, "found": False, "cards": []}


@pytest.fixture
def serve():
    """Start a service on a free loopback port and always stop it."""
    started: list[LookupService] = []

    def make(check=None) -> int:
        service = LookupService(_lookup, check=check, port=_free_port())
        assert service.start(), "the service did not bind"
        started.append(service)
        return service._port

    yield make
    for service in started:
        service.stop()


class TestCorrectingAPhrase:
    def test_the_correction_comes_back_whole(self, serve):
        port = serve(lambda text, mode, refresh: CORRECTION)

        status, body = _post(port, "/check", {"text": "I have went."})

        assert status == 200
        assert body == CORRECTION

    def test_the_phrase_the_mode_and_the_refresh_flag_all_arrive(self, serve):
        seen = {}

        def check(text, mode, refresh):
            seen.update(text=text, mode=mode, refresh=refresh)
            return CORRECTION

        port = serve(check)

        _post(
            port, "/check", {"text": "I have went.", "mode": "spoken", "refresh": True}
        )

        assert seen == {"text": "I have went.", "mode": "spoken", "refresh": True}

    def test_a_missing_mode_is_passed_through_empty_rather_than_guessed(self, serve):
        # Which register to use is the PLUGIN's decision — it holds the configured default — and
        # a service that guessed here would quietly override a setting.
        seen = {}
        port = serve(lambda text, mode, refresh: seen.update(mode=mode) or CORRECTION)

        _post(port, "/check", {"text": "x"})

        assert seen["mode"] == ""


class TestWhatItRefuses:
    def test_an_empty_phrase_is_a_400_and_never_reaches_the_model(self, serve):
        called = []
        port = serve(lambda *a: called.append(1) or CORRECTION)

        status, body = _post(port, "/check", {"text": "   "})

        assert status == 400
        assert "nothing to check" in body["error"]
        assert called == [], "an empty selection spent a request"

    def test_a_phrase_far_too_long_is_a_400_and_never_reaches_the_model(self, serve):
        # A 400, not a 502. Sent as a provider failure it would arrive at the panel dressed as
        # "the model could not be reached", and the user would go and check an API key over a
        # selection that was merely too big.
        from omnia.plugins.word_lookup.service import MAX_PHRASE_CHARS

        called = []
        port = serve(lambda *a: called.append(1) or CORRECTION)

        status, body = _post(port, "/check", {"text": "x " * MAX_PHRASE_CHARS})

        assert status == 400
        assert "too long" in body["error"]
        assert str(MAX_PHRASE_CHARS) in body["error"].replace(
            ",", ""
        ), "it refused without saying what the limit is"
        assert called == [], "an oversized selection spent a request"

    def test_a_phrase_right_at_the_limit_is_accepted(self, serve):
        from omnia.plugins.word_lookup.service import MAX_PHRASE_CHARS

        port = serve(lambda text, mode, refresh: CORRECTION)

        status, _body = _post(port, "/check", {"text": "x" * MAX_PHRASE_CHARS})

        assert status == 200, "the limit was off by one at the boundary"

    def test_a_body_that_is_not_json_is_a_400(self, serve):
        port = serve(lambda *a: CORRECTION)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/check",
            data=b"not json at all",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("it accepted a body that was not JSON")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_a_json_list_is_not_a_request(self, serve):
        port = serve(lambda *a: CORRECTION)

        status, _body = _post(port, "/check", ["text", "please"])

        assert status == 400

    def test_a_request_a_web_page_started_is_refused(self, serve):
        # Same guard as /generate, and for the same reason: fetch(no-cors) cannot read the
        # answer but still performs the side effect, and this side effect spends money.
        called = []
        port = serve(lambda *a: called.append(1) or CORRECTION)

        status, body = _post(
            port, "/check", {"text": "x"}, Origin="https://example.com"
        )

        assert status == 403
        assert "web page" in body["error"]
        assert called == []

    def test_an_extension_is_not_a_page(self, serve):
        port = serve(lambda text, mode, refresh: CORRECTION)

        status, _body = _post(
            port, "/check", {"text": "x"}, Origin="chrome-extension://abc"
        )

        assert status == 200

    def test_posting_to_an_unknown_path_is_a_404(self, serve):
        port = serve(lambda *a: CORRECTION)

        status, _body = _post(port, "/correct", {"text": "x"})

        assert status == 404


class TestTellingOffApartFromBroken:
    def test_no_checker_wired_is_a_503_that_names_the_switch(self, serve):
        # A build without Phrase Check, or one where it is switched off. Answered by turning a
        # feature on — a different sentence from a check that was tried and failed, and sending
        # one for the other is how a user hunts for a setting that was never the problem.
        port = serve(None)

        status, body = _post(port, "/check", {"text": "I have went."})

        assert status == 503
        assert "switched off" in body["error"]

    def test_the_plugin_saying_it_is_unavailable_is_also_a_503(self, serve):
        def check(*_args):
            raise PhraseCheckUnavailableError("Phrase Check is not running.")

        port = serve(check)

        status, _body = _post(port, "/check", {"text": "x"})

        assert status == 503

    def test_a_check_that_failed_is_a_502_carrying_its_reason(self, serve):
        # The provider's own sentence survives: it is the difference between "check your key"
        # and "you are out of credit", and both are things only the user can fix.
        def check(*_args):
            raise CheckFailedError(
                "Could not check that phrase — HTTP 401: invalid api key"
            )

        port = serve(check)

        status, body = _post(port, "/check", {"text": "x"})

        assert status == 502
        assert "invalid api key" in body["error"]

    def test_an_unexpected_error_is_a_500_that_leaks_nothing(self, serve):
        # A 500 and a flat sentence, the same way /generate treats a failure it did not expect.
        # An AttributeError's repr, or a provider error embedding a request URL with a key in
        # it, must not be echoed to a clipper just because it happened to be raised here.
        #
        # The previous version of this test read `status in (500, 502)` and
        # `"internal detail" not in body or status == 502`, which is true whatever the code
        # does — it asserted nothing at all.
        def check(*_args):
            raise ValueError("an internal detail nobody should see")

        port = serve(check)

        status, body = _post(port, "/check", {"text": "x"})

        assert status == 500
        assert "internal detail" not in json.dumps(body)

    def test_a_message_the_plugin_marked_for_a_person_does_reach_the_panel(self, serve):
        # The other half of the same rule. The provider's own sentence names the thing only the
        # user can fix, so an error the plugin deliberately wrote is passed through as a 502.
        class CuratedError(RuntimeError):
            user_facing = True

        def check(*_args):
            raise CuratedError(
                "Could not check that phrase — HTTP 401: invalid api key"
            )

        port = serve(check)

        status, body = _post(port, "/check", {"text": "x"})

        assert status == 502
        assert "invalid api key" in body["error"]


class TestItDoesNotDisturbTheRest:
    def test_lookup_still_works_beside_it(self, serve):
        port = serve(lambda *a: CORRECTION)

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/lookup?word=test", timeout=5
        ) as response:
            assert json.loads(response.read().decode("utf-8"))["word"] == "test"

    def test_a_slow_check_does_not_block_a_lookup(self, serve):
        # The panel's magnifier must keep answering while a correction is in flight; a model call
        # takes seconds and the clipper is the same process making both requests.
        running = threading.Event()
        release = threading.Event()

        def check(*_args):
            running.set()
            release.wait(5)
            return CORRECTION

        port = serve(check)
        worker = threading.Thread(
            target=lambda: _post(port, "/check", {"text": "x"}), daemon=True
        )
        worker.start()
        assert running.wait(5), "the check never started"
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/lookup?word=test", timeout=5
            ) as response:
                assert response.status == 200
        finally:
            release.set()
            worker.join(timeout=5)

    def test_a_check_that_raises_does_not_take_the_service_down(self, serve):
        port = serve(lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))

        _post(port, "/check", {"text": "x"})

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/lookup?word=after", timeout=5
        ) as response:
            assert response.status == 200
