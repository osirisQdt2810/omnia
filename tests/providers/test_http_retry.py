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


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"boom"))


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
    def test_retries_transient_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(503)
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().post_json_for_bytes("https://x", {}) == b"OK"
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

    def test_exhausts_retries_on_persistent_5xx(self, monkeypatch):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise _http_error(500)

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with pytest.raises(ProviderError):
            _client(max_attempts=3).post_json_for_bytes("https://x", {})
        assert calls["n"] == 3

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
                raise urllib.error.URLError("connection refused")
            return _Resp(b"OK")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert _client().post_json_for_bytes("https://x", {}) == b"OK"
        assert calls["n"] == 2
