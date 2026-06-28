"""Tests for the Vertex TokenSource strategies (signer + HTTP + clock injected)."""

from __future__ import annotations

import pytest
from conftest import FakeHttpClient

from omnia.core.providers import ProviderError
from omnia.core.providers.token_source import (
    ServiceAccountSigner,
    StaticTokenSource,
    resolve_token_source,
)


class _FakeSigner:
    def __init__(self) -> None:
        self.count = 0

    def sign(self, message: bytes, private_key_pem: str) -> bytes:
        self.count += 1
        return b"signature-bytes"


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestResolveTokenSource:
    def test_resolve_static_token_source(self):
        src = resolve_token_source({"access_token": "abc"}, FakeHttpClient())
        assert isinstance(src, StaticTokenSource)
        assert src.token() == "abc"

    def test_resolve_no_credentials_raises(self):
        with pytest.raises(ProviderError):
            resolve_token_source({}, FakeHttpClient())

    def test_resolve_use_gcloud_without_creds_raises_no_shell_out(self):
        # The gcloud CLI fallback was removed (the add-on never shells out): a config that only
        # asks for gcloud now raises the clear "needs credentials" error instead of running a CLI.
        with pytest.raises(ProviderError):
            resolve_token_source({"use_gcloud": True}, FakeHttpClient())


def _sa_config():
    return {
        "credentials_json": {
            "client_email": "svc@project.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


class TestServiceAccountTokenSource:
    def test_service_account_mints_and_caches(self):
        http = FakeHttpClient(json={"access_token": "minted", "expires_in": 3600})
        signer = _FakeSigner()
        src = resolve_token_source(
            _sa_config(), http, signer=signer, clock=_Clock(1000.0)
        )
        assert src.token() == "minted"
        assert src.token() == "minted"  # cached
        assert signer.count == 1
        # Token exchange must be form-urlencoded (post_form), not JSON — Google rejects
        # a JSON body.
        assert len([c for c in http.calls if c[0] == "post_form"]) == 1
        # the JWT assertion + grant_type were posted to the token URI
        method, url, fields, _ = http.calls[0]
        assert method == "post_form"
        assert url.endswith("/token")
        assert fields["grant_type"].endswith("jwt-bearer")
        assert "assertion" in fields

    def test_service_account_refreshes_after_expiry(self):
        http = FakeHttpClient(json={"access_token": "tok", "expires_in": 3600})
        signer = _FakeSigner()
        clock = _Clock(1000.0)
        src = resolve_token_source(_sa_config(), http, signer=signer, clock=clock)
        src.token()
        clock.now = 1000.0 + 4000  # past expiry
        src.token()
        assert signer.count == 2  # re-minted


class TestServiceAccountSigner:
    def test_signer_pem_to_der_strips_headers(self):
        pem = "-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----\n"
        assert ServiceAccountSigner._pem_to_der(pem) == b"ABC"
