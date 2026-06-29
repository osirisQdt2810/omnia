"""Vertex AI access-token acquisition — a Strategy pattern.

A :class:`TokenSource` knows how to produce a bearer token; concrete strategies cover the
ways the add-on authenticates to Vertex WITHOUT shelling out (Anki can't run a CLI reliably
on every host):

* :class:`StaticTokenSource` — a token supplied directly in config.
* :class:`ServiceAccountTokenSource` — mints + caches a token from a service-account JSON
  (RS256-signed JWT via :class:`ServiceAccountSigner`; needs vendored ``rsa`` + ``pyasn1``).

:func:`resolve_token_source` picks the strategy from config. Collaborators (the HTTP client,
the signer, the clock) are injected, so the whole module is testable without real creds.
"""

from __future__ import annotations

import base64
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Optional

from omnia.core.network.http import HttpClient
from omnia.core.providers.errors import ProviderError

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_TOKEN_SKEW_S = 60  # refresh this many seconds before actual expiry


class TokenSource(ABC):
    """Produces a Vertex bearer token."""

    @abstractmethod
    def token(self) -> str:
        """Return a currently-valid access token (raises ProviderError on failure)."""


class StaticTokenSource(TokenSource):
    """A token supplied verbatim in config."""

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    def token(self) -> str:
        return self._token


class ServiceAccountSigner:
    """RS256-signs bytes with a service-account PKCS#8 PEM key (pure-Python rsa + pyasn1)."""

    def sign(self, message: bytes, private_key_pem: str) -> bytes:
        """Return the RS256 signature of ``message`` under ``private_key_pem``."""
        try:
            import rsa
        except ImportError as exc:  # pragma: no cover - only without the vendored dep
            raise ProviderError(
                "Service-account auth needs the vendored 'rsa' package. Add 'rsa' + 'pyasn1' "
                "to requirements-vendor.txt and run scripts/vendor_deps.py, or paste an "
                "'access_token' instead."
            ) from exc
        return rsa.sign(message, self._load_key(private_key_pem), "SHA-256")

    @staticmethod
    def _load_key(pem: str) -> Any:
        import rsa

        der = ServiceAccountSigner._pem_to_der(pem)
        try:
            from pyasn1.codec.der import decoder as der_decoder
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "Service-account auth needs the vendored 'pyasn1' package (PKCS#8 unwrap)."
            ) from exc
        info, _ = der_decoder.decode(der)
        pkcs1_der = bytes(
            info[2]
        )  # PrivateKeyInfo.privateKey (OCTET STRING) == PKCS#1 DER
        return rsa.PrivateKey.load_pkcs1(pkcs1_der, format="DER")

    @staticmethod
    def _pem_to_der(pem: str) -> bytes:
        lines = [ln for ln in pem.strip().splitlines() if "-----" not in ln]
        return base64.b64decode("".join(lines))


class ServiceAccountTokenSource(TokenSource):
    """Mints and caches a token from a service-account JSON via a signed JWT exchange."""

    def __init__(
        self,
        service_account: dict[str, Any],
        http: HttpClient,
        signer: Optional[ServiceAccountSigner] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sa = service_account
        self._http = http
        self._signer = signer or ServiceAccountSigner()
        self._clock = clock
        self._cached: Optional[str] = None
        self._expiry: float = 0.0

    def token(self) -> str:
        now = self._clock()
        if self._cached and self._expiry - _TOKEN_SKEW_S > now:
            return self._cached
        self._cached, self._expiry = self._mint(now)
        return self._cached

    def _mint(self, now: float) -> tuple[str, float]:
        token_uri = self._sa.get("token_uri", "https://oauth2.googleapis.com/token")
        issued = int(now)
        claim = {
            "iss": self._sa["client_email"],
            "scope": _SCOPE,
            "aud": token_uri,
            "iat": issued,
            "exp": issued + 3600,
        }
        signing_input = b".".join((_b64url(_HEADER), _b64url(claim)))
        signature = self._signer.sign(signing_input, self._sa["private_key"])
        assertion = b".".join((signing_input, _b64url_bytes(signature))).decode("ascii")
        # Google's OAuth2 token endpoint requires a form-urlencoded body (a JSON body is
        # silently misread, yielding "unsupported_grant_type") — so use post_form, not post_json.
        resp = self._http.post_form(
            token_uri,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        access_token = resp.get("access_token")
        if not access_token:
            raise ProviderError(
                f"Vertex token exchange returned no access_token: {resp}"
            )
        return str(access_token), now + int(resp.get("expires_in", 3600))


def resolve_token_source(
    config: dict[str, Any],
    http: HttpClient,
    *,
    signer: Optional[ServiceAccountSigner] = None,
    clock: Callable[[], float] = time.time,
) -> TokenSource:
    """Pick the auth strategy from ``config`` (access_token > service account JWT mint).

    The add-on never shells out to ``gcloud`` (Anki can't rely on a CLI being installed); the
    service-account JWT exchange over HTTP is the credential path. ``use_gcloud`` is no longer
    honored — supply a service-account JSON or a pasted ``access_token`` instead.
    """
    if config.get("access_token"):
        return StaticTokenSource(str(config["access_token"]))
    service_account = _load_service_account(config)
    if service_account is not None:
        return ServiceAccountTokenSource(service_account, http, signer, clock)
    raise ProviderError(
        "gemini_vertex needs credentials: set 'access_token' or 'credentials_path'/"
        "'credentials_json' (a service-account JSON). The 'gcloud' CLI is not used."
    )


def _load_service_account(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = config.get("credentials_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return json.loads(raw)
    path = config.get("credentials_path")
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return None


def service_account_project(config: dict[str, Any]) -> str:
    """Return the GCP ``project_id`` from the configured service-account JSON, or ``""``.

    Lets a Vertex config omit ``project`` (the service-account key already carries it). Any
    problem reading/parsing the credentials yields ``""`` — the provider then raises its own
    clear "requires a GCP 'project'" error rather than this surfacing an obscure one.
    """
    try:
        service_account = _load_service_account(config)
    except (OSError, ValueError):
        return ""
    return str(service_account.get("project_id", "")) if service_account else ""


_HEADER = {"alg": "RS256", "typ": "JWT"}


def _b64url(obj: Any) -> bytes:
    return _b64url_bytes(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _b64url_bytes(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")
