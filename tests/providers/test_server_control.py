"""Tests for the endpoint's remote control (``core/providers/server_control.py``).

A server of the user's own (omnia-llm) keeps a model on the GPU until it has sat idle for a
while. These endpoints let the Keys card free the GPU at once and change how long "a while" is.
They are an extension: a stock OpenAI-compatible server answers 404, which is a fact to show
quietly, not an error.
"""

from __future__ import annotations

import json

import pytest

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.server_control import (
    RemoteControlUnsupportedError,
    ServerControl,
    ServerSettings,
)

_SETTINGS = {"idle_timeout_minutes": 30, "default_idle_timeout_minutes": 30}


class _Http:
    """Records each request; answers with ``answer``, or raises it when it is an error."""

    def __init__(self, answer=None):
        self.answer = _SETTINGS if answer is None else answer
        self.calls: list[tuple[str, str, object, dict]] = []

    def _reply(self, method, url, payload, headers):
        self.calls.append((method, url, payload, headers or {}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def get_json(self, url, *, params=None, headers=None):
        return self._reply("GET", url, None, headers)

    def put_json(self, url, payload, *, headers=None):
        return self._reply("PUT", url, payload, headers)

    def post_json(self, url, payload, *, headers=None):
        return self._reply("POST", url, payload, headers)


def _refused(code: int, url: str, body: str) -> ProviderError:
    """The error ``UrllibHttpClient`` raises for an HTTP error answer."""
    return ProviderError(f"HTTP {code} from {url}: {body}", status_code=code)


class TestTheServerRoot:
    @pytest.mark.parametrize(
        ("base_url", "root"),
        [
            ("https://host/v1", "https://host"),
            ("https://host/v1/", "https://host"),
            ("https://host", "https://host"),
            ("https://host/", "https://host"),
            ("https://host/custom", "https://host/custom"),
            ("https://host/api/v1", "https://host/api"),
        ],
    )
    def test_the_api_path_is_dropped_and_nothing_else(self, base_url, root):
        assert ServerControl.root_url(base_url) == root

    def test_a_path_that_only_ends_like_v1_is_kept(self):
        assert ServerControl.root_url("https://host/apiv1") == "https://host/apiv1"


class TestTheRequests:
    def test_settings_are_read_from_the_root_with_the_key(self):
        http = _Http()

        got = ServerControl("https://h/v1", "tok", http).settings()

        assert got == ServerSettings(30, 30)
        assert http.calls == [
            ("GET", "https://h/settings", None, {"Authorization": "Bearer tok"})
        ]

    def test_the_idle_timeout_is_put_as_a_whole_number(self):
        http = _Http({"idle_timeout_minutes": 5, "default_idle_timeout_minutes": 30})

        got = ServerControl("https://h/v1/", "tok", http).set_idle_timeout(5)

        assert got == ServerSettings(5, 30)
        method, url, payload, headers = http.calls[0]
        assert (method, url, payload) == (
            "PUT",
            "https://h/settings",
            {"idle_timeout_minutes": 5},
        )
        assert headers["Authorization"] == "Bearer tok"

    def test_stop_is_posted_to_the_root(self):
        http = _Http({"stopped": True})

        ServerControl("https://h/v1", "tok", http).stop()

        assert http.calls == [
            ("POST", "https://h/stop", {}, {"Authorization": "Bearer tok"})
        ]

    def test_a_fractional_timeout_from_the_server_is_kept(self):
        """The server's own config may say 7.5; rounding it here would misreport it."""
        http = _Http({"idle_timeout_minutes": 7.5, "default_idle_timeout_minutes": 30})

        assert (
            ServerControl("https://h", "k", http).settings().idle_timeout_minutes == 7.5
        )

    def test_an_answer_that_is_not_settings_says_so(self):
        http = _Http({"detail": "hello"})

        with pytest.raises(ProviderError, match="settings"):
            ServerControl("https://h", "k", http).settings()


class TestWhenTheServerSaysNo:
    @pytest.mark.parametrize("call", ["settings", "stop", "set_idle_timeout"])
    def test_a_404_means_the_server_has_no_remote_control(self, call):
        http = _Http(_refused(404, "https://h/x", '{"detail": "Not Found"}'))
        control = ServerControl("https://h/v1", "k", http)

        with pytest.raises(RemoteControlUnsupportedError):
            if call == "set_idle_timeout":
                control.set_idle_timeout(5)
            else:
                getattr(control, call)()

    def test_a_range_error_carries_the_servers_own_message(self):
        message = "idle_timeout_minutes must be a whole number from 1 to 1440"
        body = json.dumps({"error": {"message": message, "type": "bad_request"}})
        http = _Http(_refused(400, "https://h/settings", body))

        with pytest.raises(ProviderError) as caught:
            ServerControl("https://h/v1", "k", http).set_idle_timeout(5000)

        assert str(caught.value) == message
        assert caught.value.status_code == 400
        assert not isinstance(caught.value, RemoteControlUnsupportedError)

    def test_an_error_without_a_json_message_is_passed_on_whole(self):
        error = _refused(401, "https://h/settings", "invalid or missing API key")
        http = _Http(error)

        with pytest.raises(ProviderError) as caught:
            ServerControl("https://h", "bad", http).settings()

        assert caught.value is error
