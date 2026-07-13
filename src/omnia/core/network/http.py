"""HTTP client abstraction for the provider layer.

An :class:`HttpClient` (ABC) defines the small surface providers need; :class:`UrllibHttpClient`
is the stdlib-only implementation (so the add-on needs nothing vendored for HTTP, and runs
identically on macOS + Windows). Providers depend on the abstraction and receive a client by
injection (DIP), so tests pass a fake instead of monkeypatching globals.

Calls are blocking — run them off the Qt main thread (see CONVENTIONS Part 2 → Threading).
All failures raise :class:`~omnia.core.providers.errors.ProviderError`.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from omnia.core.providers.errors import ProviderError

DEFAULT_TIMEOUT = 60

# HTTP methods safe to retry on a 5xx: a 5xx may mean the server DID process a non-idempotent
# request, so retrying a POST risks a double-charge (e.g. a duplicate LLM/TTS billing). GET/HEAD
# carry no side effects, so retrying them on transient 5xx is safe.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _parse_retry_after(headers: Any) -> Optional[float]:
    """Return the ``Retry-After`` delay in seconds, or None when absent/non-numeric.

    Only the delta-seconds form is honored (the form these APIs use for 429/503); the rarer
    HTTP-date form falls back to the caller's backoff (returns None).
    """
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    return None


@dataclass
class RetryPolicy:
    """Exponential-backoff-with-jitter retry on transient HTTP failures.

    Adapted from vio-ai's shared HTTP retry: retries 429 + 5xx responses and network
    errors. Collaborators (``sleep``, ``jitter``) are injected so it's testable without
    real delays.
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    retriable_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = field(default=lambda: random.uniform(0, 1.0))

    def delay_for(self, attempt: int) -> float:
        """Return the backoff delay (seconds) before retry ``attempt`` (0-indexed)."""
        return min(self.base_delay * (2**attempt) + self.jitter(), self.max_delay)


class HttpClient(ABC):
    """The HTTP surface the provider layer depends on."""

    @abstractmethod
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """POST ``payload`` as JSON; return the parsed JSON object response."""

    @abstractmethod
    def post_form(
        self,
        url: str,
        fields: dict[str, str],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """POST ``fields`` as ``application/x-www-form-urlencoded``; return the JSON object.

        Required by OAuth2 token endpoints (e.g. Google's), which reject a JSON body.
        """

    @abstractmethod
    def post_json_for_bytes(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> bytes:
        """POST ``payload`` as JSON; return the raw response bytes (e.g. audio)."""

    @abstractmethod
    def get_bytes(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> bytes:
        """GET ``url`` (with optional query ``params``); return the raw response bytes."""

    @abstractmethod
    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """GET ``url`` (with optional query ``params``); return the parsed JSON object."""


class UrllibHttpClient(HttpClient):
    """Stdlib (``urllib``) implementation of :class:`HttpClient`."""

    def __init__(
        self, timeout: int = DEFAULT_TIMEOUT, retry: Optional[RetryPolicy] = None
    ) -> None:
        self._timeout = timeout
        self._retry = retry or RetryPolicy()

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        return self._parse_json_object(
            self._request(self._build_post(url, payload, headers)), url
        )

    def post_form(
        self,
        url: str,
        fields: dict[str, str],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        return self._parse_json_object(
            self._request(self._build_form(url, fields, headers)), url
        )

    def post_json_for_bytes(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> bytes:
        return self._request(self._build_post(url, payload, headers))

    def get_bytes(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        return self._request(req)

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        full = url
        if params:
            full = f"{url}?{urllib.parse.urlencode(params)}"
        return self._parse_json_object(
            self.get_bytes(url, params=params, headers=headers), full
        )

    @staticmethod
    def _build_post(
        url: str, payload: dict[str, Any], headers: Optional[dict[str, str]]
    ) -> urllib.request.Request:
        body = json.dumps(payload).encode("utf-8")
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        return urllib.request.Request(
            url, data=body, headers=all_headers, method="POST"
        )

    @staticmethod
    def _build_form(
        url: str, fields: dict[str, str], headers: Optional[dict[str, str]]
    ) -> urllib.request.Request:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        all_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        }
        return urllib.request.Request(
            url, data=body, headers=all_headers, method="POST"
        )

    @staticmethod
    def _parse_json_object(raw: bytes, url: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ProviderError(f"Invalid JSON response from {url}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                f"Expected a JSON object from {url}, got {type(parsed).__name__}"
            )
        return parsed

    def _request(self, req: urllib.request.Request) -> bytes:
        retry = self._retry
        # A non-idempotent method (POST) retries only on 429 (rate-limited — rejected, not
        # processed) + network errors, NEVER on 5xx (which may mean the request WAS processed →
        # a retry double-charges). Idempotent GET/HEAD retry the full transient set.
        if req.get_method() in _IDEMPOTENT_METHODS:
            retriable = retry.retriable_statuses
        else:
            retriable = tuple(code for code in retry.retriable_statuses if code == 429)
        for attempt in range(retry.max_attempts):
            last = attempt >= retry.max_attempts - 1
            retry_after: Optional[float] = None
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if last or exc.code not in retriable:
                    body = exc.read().decode("utf-8", "replace")[:500]
                    raise ProviderError(
                        f"HTTP {exc.code} from {req.full_url}: {body}",
                        status_code=exc.code,
                    ) from exc
                # Honor a server-supplied Retry-After (429/503) over the computed backoff.
                retry_after = _parse_retry_after(exc.headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if last:
                    raise ProviderError(
                        f"Network error calling {req.full_url}: {exc}"
                    ) from exc
            delay = retry_after if retry_after is not None else retry.delay_for(attempt)
            retry.sleep(min(delay, retry.max_delay))
        raise ProviderError(
            f"Request to {req.full_url} exhausted retries"
        )  # unreachable


# Process-wide default; providers fall back to this when none is injected.
DEFAULT_HTTP_CLIENT: HttpClient = UrllibHttpClient()
