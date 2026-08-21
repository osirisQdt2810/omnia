"""Tests for UrllibHttpClient transient-retry (adapted from vio-ai's HTTP retry)."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from omnia.core.network.http import RetryPolicy, UrllibHttpClient
from omnia.core.providers.errors import ProviderError


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self) -> bytes:
        return self._data


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://x", code, "err", headers or {}, io.BytesIO(b"boom")
    )


def _client(max_attempts: int = 3) -> UrllibHttpClient:
    # No real sleep, no jitter -> fast, deterministic.
    return UrllibHttpClient(
        retry=RetryPolicy(
            max_attempts=max_attempts,
            base_delay=0,
            sleep=lambda _s: None,
            jitter=lambda: 0.0,
        )
    )


class TestUrllibHttpClientRetry:
    def test_get_retries_transient_5xx_then_succeeds(self, monkeypatch):
        # GET is idempotent, so a transient 5xx is safe to retry.
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(503)
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().get_bytes("https://x") == b"OK"
        assert calls["n"] == 3

    def test_non_retriable_status_raises_immediately(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise _http_error(400)

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client().post_json_for_bytes("https://x", {})
        assert calls["n"] == 1  # not retried

    def test_get_exhausts_retries_on_persistent_5xx(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise _http_error(500)

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client(max_attempts=3).get_bytes("https://x")
        assert calls["n"] == 3

    def test_post_is_not_retried_on_5xx(self, monkeypatch):
        # A 5xx may mean the POST WAS processed; retrying risks a double-charge, so a POST must
        # fail immediately on 5xx instead of retrying.
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise _http_error(503)

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client().post_json_for_bytes("https://x", {})
        assert calls["n"] == 1  # POST not retried on 5xx

    def test_post_is_retried_on_429(self, monkeypatch):
        # 429 (rate-limited) means the request was rejected, not processed — safe to retry a POST.
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(429)
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().post_json_for_bytes("https://x", {}) == b"OK"
        assert calls["n"] == 3

    def test_honors_retry_after_header_over_backoff(self, monkeypatch):
        # A server-supplied Retry-After (seconds) is honored as the delay instead of the backoff.
        slept: list[float] = []
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(429, headers={"Retry-After": "2"})
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        client = UrllibHttpClient(
            retry=RetryPolicy(
                base_delay=0,
                sleep=slept.append,
                jitter=lambda: 0.0,
            )
        )
        assert client.post_json_for_bytes("https://x", {}) == b"OK"
        assert slept == [2.0]  # the Retry-After value, not the (0) backoff

    def test_post_form_sends_urlencoded_body(self, monkeypatch):
        # Regression: the OAuth2 token exchange must send a form-urlencoded body, not
        # JSON — a JSON body is silently misread by Google's /token endpoint (empty
        # grant_type -> 400).
        captured = {}

        def fake(req, timeout=None):
            captured["ctype"] = req.get_header("Content-type")
            captured["body"] = req.data
            return _Resp(b'{"access_token": "x", "expires_in": 3600}')

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        out = _client().post_form(
            "https://oauth2.googleapis.com/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": "JWT",
            },
        )
        assert out["access_token"] == "x"
        assert captured["ctype"] == "application/x-www-form-urlencoded"
        assert captured["body"] == (
            b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
            b"&assertion=JWT"
        )

    def test_network_error_is_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                # urllib wraps the underlying OSError as the URLError's ``reason`` — and the
                # reason is what says whether the request ever left this machine.
                raise urllib.error.URLError(
                    ConnectionRefusedError("connection refused")
                )
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().post_json_for_bytes("https://x", {}) == b"OK"
        assert calls["n"] == 2

    def test_a_post_read_timeout_is_retried_and_can_succeed(self, monkeypatch):
        """A hotel-wifi timeout on a completion must not lose the field on the first try.

        The ambiguity is real (the request went out, the answer did not come back, so a retry
        MAY pay twice) but refusing to retry does not avoid that cost — the user re-runs the
        field and pays anyway, having first been shown an error.
        """
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimeoutError("read timed out")
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().post_json_for_bytes("https://x", {}) == b"OK"
        assert calls["n"] == 2

    def test_a_post_keeps_the_full_network_retry_budget(self, monkeypatch):
        """A POST retries a read timeout the FULL budget — the same as a GET, as it always has.

        This pins an intent, not a discovery. Bounding a POST to one network retry (to cap a
        possible double-charge on a request that may already have been applied) was written and
        reverted: it costs a field that the third attempt would have generated, it does not
        actually save the charge, and changing how every provider on every path retries is not
        something a throughput change gets to do quietly. If this test starts asserting two
        attempts, that decision is being reversed — do it deliberately, in its own change, with
        a release note.
        """
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("read timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client().post_json_for_bytes("https://x", {})
        assert calls["n"] == 3

    def test_a_post_refused_connection_keeps_the_full_retry_budget(self, monkeypatch):
        """A refused connection is provably PRE-send: retrying it cannot double-charge."""
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client().post_json_for_bytes("https://x", {})
        assert calls["n"] == 3

    def test_a_get_exhausts_the_full_budget_on_read_timeouts(self, monkeypatch):
        """An idempotent request exhausts the budget too — the 5xx narrowing is method-only."""
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("read timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client().get_bytes("https://x")
        assert calls["n"] == 3

    def test_a_get_read_timeout_is_still_retried(self, monkeypatch):
        """GET carries no side effect, so the ambiguity does not exist and retry is free."""
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimeoutError("read timed out")
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().get_bytes("https://x") == b"OK"
        assert calls["n"] == 2
