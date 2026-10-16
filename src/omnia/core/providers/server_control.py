"""Remote control of a self-hosted endpoint: free its GPU now, set how long a model stays.

A server of the user's own (omnia-llm) loads a model onto the GPU on first use and stops it
after it has sat idle for a while. Three optional endpoints let Omnia steer that from the Keys
card, all at the server ROOT rather than under the OpenAI API path:

- ``GET  {root}/settings`` -> ``{"idle_timeout_minutes", "default_idle_timeout_minutes"}``
- ``PUT  {root}/settings`` with ``{"idle_timeout_minutes": N}`` (1..1440) -> the same shape
- ``POST {root}/stop`` -> ``{"stopped": true}``

They are an extension, not part of the OpenAI shape: a stock server (vLLM, Ollama, LM Studio)
answers 404, which :class:`RemoteControlUnsupportedError` reports as a fact about the server rather
than as a failure. Pure: no ``aqt``/``anki``; the HTTP client is injected. Calls block, so run
them off the Qt main thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnia.core.network.http import HttpClient
from omnia.core.providers.errors import ProviderError


class RemoteControlUnsupportedError(ProviderError):
    """The server answered 404: it has no remote control. Not an error to show as one."""


@dataclass(frozen=True)
class ServerSettings:
    """How long an idle model keeps its GPU, now and by the server's own default (minutes).

    Kept as the server's numbers, fractions included: its config file may say 7.5, and only a
    value Omnia sends is held to whole minutes.
    """

    idle_timeout_minutes: float
    default_idle_timeout_minutes: float

    @classmethod
    def from_answer(cls, payload: Any) -> ServerSettings:
        """Read a ``/settings`` answer.

        Raises:
            ProviderError: when the answer is not one.
        """
        values: list[float] = []
        for key in ("idle_timeout_minutes", "default_idle_timeout_minutes"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ProviderError(f"Not a settings answer: {str(payload)[:200]}")
            values.append(value)
        return cls(values[0], values[1])

    def to_payload(self) -> dict[str, float]:
        """The shape the page receives — the server's own."""
        return {
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "default_idle_timeout_minutes": self.default_idle_timeout_minutes,
        }


class ServerControl:
    """The remote-control endpoints of one server, addressed by the card's Base URL + key."""

    def __init__(self, base_url: str, api_key: str, http: HttpClient) -> None:
        self._root = self.root_url(base_url)
        self._api_key = api_key
        self._http = http

    @staticmethod
    def root_url(base_url: str) -> str:
        """The server root: ``base_url`` without trailing slashes and one trailing ``/v1``.

        Only a whole ``/v1`` segment goes; any other path (``/custom``, the ``/api`` of
        ``/api/v1``) is where the user put the server, so it stays.
        """
        url = base_url.strip().rstrip("/")
        if url.endswith("/v1"):
            url = url[: -len("/v1")].rstrip("/")
        return url

    def settings(self) -> ServerSettings:
        """The idle timeout now in force, and the server's default."""
        url = f"{self._root}/settings"
        return ServerSettings.from_answer(
            self._call(url, lambda: self._http.get_json(url, headers=self._headers()))
        )

    def set_idle_timeout(self, minutes: int) -> ServerSettings:
        """Keep an idle model on the GPU for ``minutes`` (the server accepts 1..1440).

        The range is the server's to enforce: its refusal comes back with its own message.
        """
        url = f"{self._root}/settings"
        payload = {"idle_timeout_minutes": minutes}
        return ServerSettings.from_answer(
            self._call(
                url,
                lambda: self._http.put_json(url, payload, headers=self._headers()),
            )
        )

    def stop(self) -> None:
        """Stop every running model on the server now, freeing its GPU."""
        url = f"{self._root}/stop"
        self._call(url, lambda: self._http.post_json(url, {}, headers=self._headers()))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _call(url: str, request: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run ``request``; a 404 becomes :class:`RemoteControlUnsupportedError`, and a refusal
        carrying ``{"error": {"message"}}`` is re-raised with just that message.

        The message is what the user can act on ("must be from 1 to 1440"); the HTTP client's
        own wording wraps it in a status line and the full address.
        """
        try:
            return request()
        except ProviderError as exc:
            if exc.status_code == 404:
                raise RemoteControlUnsupportedError(
                    "This server does not support remote control.", status_code=404
                ) from exc
            message = ServerControl._server_message(exc, url)
            if not message:
                raise
            raise ProviderError(message, status_code=exc.status_code) from exc

    @staticmethod
    def _server_message(exc: ProviderError, url: str) -> str:
        """The ``error.message`` of the body behind ``exc``, or ``""`` when there is none."""
        prefix = f"HTTP {exc.status_code} from {url}: "
        text = str(exc)
        if not text.startswith(prefix):
            return ""
        try:
            body = json.loads(text[len(prefix) :])
        except ValueError:
            return ""
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        return str(message) if message else ""
