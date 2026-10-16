"""GPU controls on an endpoint card: stop the server's models now, and set the idle timeout.

The controller half (two ops, run off the collection thread, pushing to
``window.__snEndpointServer``) and the page half (the shipped receiver, run in node over a DOM
just big enough). The HTTP itself is ``ServerControl``'s, tested in
``tests/providers/test_server_control.py``; here it is replaced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import types
from pathlib import Path

import pytest
from aqt_stubs import install_gui_stubs

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.server_control import (
    RemoteControlUnsupportedError,
    ServerControl,
    ServerSettings,
)

# The Account controller's module imports Qt widgets; there is no display here.
install_gui_stubs()


class TestTheControllerOps:
    """The ops, with the background runner made synchronous and the server faked."""

    def _controller(self, config_dir, monkeypatch, answer=None):
        from omnia.core import anki_compat
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        runs: list[dict] = []
        seen: dict = {}

        def run_now(
            fn, on_success=None, on_failure=None, label="", uses_collection=True
        ):
            runs.append({"uses_collection": uses_collection})
            try:
                result = fn()
            except Exception as exc:
                on_failure(exc)
            else:
                on_success(result)

        def fake(name):
            def call(self, *args):
                seen.update(
                    root=self._root, api_key=self._api_key, http=self._http, call=name
                )
                seen["args"] = args
                if isinstance(answer, Exception):
                    raise answer
                return answer

            return call

        monkeypatch.setattr(anki_compat, "run_in_background", run_now)
        for name in ("settings", "set_idle_timeout", "stop"):
            monkeypatch.setattr(ServerControl, name, fake(name))
        evals: list[str] = []
        ctx = types.SimpleNamespace(
            repo=ConfigRepository(ConfigLoader(config_dir)),
            eval_js=evals.append,
            friendly=lambda exc, prefix: f"{prefix}: {exc}",
        )
        self.runs, self.seen = runs, seen
        return AccountController(ctx), evals

    @staticmethod
    def _pushed(evals):
        match = re.fullmatch(r"window\.__snEndpointServer\((.*?), (.*)\);", evals[-1])
        assert match, evals[-1]
        return json.loads(match.group(1)), json.loads(match.group(2))

    def test_reading_the_settings_pushes_them(self, config_dir, monkeypatch):
        controller, evals = self._controller(
            config_dir, monkeypatch, ServerSettings(30, 30)
        )

        controller.on_endpoint_server_settings(
            {"provider": "custom:gpu", "base_url": "https://h/v1", "api_key": "tok"}
        )

        assert self.seen["call"] == "settings"
        assert self._pushed(evals) == (
            "custom:gpu",
            {"idle_timeout_minutes": 30, "default_idle_timeout_minutes": 30},
        )

    def test_given_minutes_it_saves_them_and_says_so(self, config_dir, monkeypatch):
        controller, evals = self._controller(
            config_dir, monkeypatch, ServerSettings(5, 30)
        )

        controller.on_endpoint_server_settings(
            {
                "provider": "custom:gpu",
                "base_url": "https://h/v1",
                "api_key": "tok",
                "minutes": "5",
            }
        )

        assert (self.seen["call"], self.seen["args"]) == ("set_idle_timeout", (5,))
        assert self._pushed(evals)[1] == {
            "idle_timeout_minutes": 5,
            "default_idle_timeout_minutes": 30,
            "saved": True,
        }

    def test_minutes_that_are_not_a_number_are_said_plainly(
        self, config_dir, monkeypatch
    ):
        controller, evals = self._controller(config_dir, monkeypatch)

        controller.on_endpoint_server_settings(
            {"provider": "p", "base_url": "https://h", "api_key": "k", "minutes": "x"}
        )

        assert "whole number" in self._pushed(evals)[1]["error"]
        assert self.seen == {}

    def test_stop_pushes_stopped(self, config_dir, monkeypatch):
        controller, evals = self._controller(config_dir, monkeypatch)

        controller.on_endpoint_stop_gpu(
            {"provider": "custom:gpu", "base_url": "https://h/v1", "api_key": "tok"}
        )

        assert self.seen["call"] == "stop"
        assert self._pushed(evals) == ("custom:gpu", {"stopped": True})

    def test_a_server_without_remote_control_is_unsupported_not_an_error(
        self, config_dir, monkeypatch
    ):
        controller, evals = self._controller(
            config_dir, monkeypatch, RemoteControlUnsupportedError("HTTP 404")
        )

        controller.on_endpoint_stop_gpu(
            {"provider": "p", "base_url": "https://h/v1", "api_key": "k"}
        )

        assert self._pushed(evals)[1] == {"unsupported": True}

    def test_a_refusal_carries_the_servers_message(self, config_dir, monkeypatch):
        controller, evals = self._controller(
            config_dir,
            monkeypatch,
            ProviderError("must be from 1 to 1440", status_code=400),
        )

        controller.on_endpoint_server_settings(
            {"provider": "p", "base_url": "https://h", "api_key": "k", "minutes": 0}
        )

        assert "must be from 1 to 1440" in self._pushed(evals)[1]["error"]

    @pytest.mark.parametrize(
        "op", ["on_endpoint_server_settings", "on_endpoint_stop_gpu"]
    )
    def test_it_never_runs_on_the_collection_thread(self, config_dir, monkeypatch, op):
        controller, _evals = self._controller(
            config_dir, monkeypatch, ServerSettings(30, 30)
        )

        getattr(controller, op)(
            {"provider": "p", "base_url": "https://h", "api_key": "k"}
        )

        assert self.runs == [{"uses_collection": False}]

    def test_what_is_typed_is_used_before_it_is_saved(self, config_dir, monkeypatch):
        controller, _evals = self._controller(config_dir, monkeypatch)

        controller.on_endpoint_stop_gpu(
            {
                "provider": "openai_compatible",
                "base_url": "https://typed/v1",
                "api_key": "typed",
            }
        )

        assert (self.seen["root"], self.seen["api_key"]) == ("https://typed", "typed")

    def test_the_saved_address_and_key_are_the_fallback(self, config_dir, monkeypatch):
        controller, _evals = self._controller(config_dir, monkeypatch)
        sub = types.SimpleNamespace(base_url="https://saved/v1", api_key="saved-key")
        monkeypatch.setattr(
            controller._ctx.repo,
            "llm_settings",
            lambda: types.SimpleNamespace(subsection=lambda _p: sub),
        )

        controller.on_endpoint_stop_gpu({"provider": "custom:gpu"})

        assert (self.seen["root"], self.seen["api_key"]) == (
            "https://saved",
            "saved-key",
        )

    @pytest.mark.parametrize(
        ("data", "said"),
        [
            ({"base_url": "", "api_key": "k"}, "Base URL"),
            ({"base_url": "https://h/v1", "api_key": ""}, "API key"),
        ],
    )
    @pytest.mark.parametrize(
        "op", ["on_endpoint_server_settings", "on_endpoint_stop_gpu"]
    )
    def test_a_missing_url_or_key_is_said_plainly_and_nothing_is_asked(
        self, config_dir, monkeypatch, data, said, op
    ):
        controller, evals = self._controller(config_dir, monkeypatch)

        getattr(controller, op)({"provider": "custom:gpu", **data})

        assert said in self._pushed(evals)[1]["error"]
        assert self.seen == {} and self.runs == []

    def test_an_automatic_read_gives_up_fast_and_a_click_is_patient(
        self, config_dir, monkeypatch
    ):
        controller, _evals = self._controller(
            config_dir, monkeypatch, ServerSettings(30, 30)
        )
        address = {"provider": "p", "base_url": "https://h", "api_key": "k"}

        controller.on_endpoint_server_settings({**address, "auto": True})
        quick = self.seen["http"]
        controller.on_endpoint_stop_gpu(address)
        patient = self.seen["http"]

        assert quick._timeout <= 10 and quick._retry.max_attempts == 1
        assert patient._timeout > quick._timeout


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
class TestTheCardShowsTheServer:
    """The page half: the shipped receiver, run in node over a DOM just big enough."""

    def _run(self, payload, idle=""):
        source = (
            Path(__file__).resolve().parents[2]
            / "src/omnia/gui/smart_notes/web/05-handlers.js"
        ).read_text(encoding="utf-8")
        receiver = re.search(
            r"  window\.__snEndpointServer = function.*?\n  \};", source, re.S
        )
        assert receiver, "__snEndpointServer moved"
        script = f"""
function node(props) {{ return Object.assign({{className: "", textContent: "", value: "",
  hidden: false, disabled: true}}, props); }}
const parts = {{
  ".sn-key-server-note": node({{}}),
  ".sn-key-server-actions": node({{}}),
  ".sn-key-idle": node({{value: {json.dumps(idle)}}}),
  ".sn-key-idle-apply": node({{}}),
  ".sn-key-stop-gpu": node({{}}),
}};
const card = node({{dataset: {{provider: "custom:gpu"}},
  querySelector(s) {{ return parts[s] || null; }} }});
const document = {{ querySelectorAll() {{ return [card]; }} }};
const window = {{}};
{receiver.group(0).strip()}
window.__snEndpointServer("custom:gpu", {json.dumps(payload)});
console.log(JSON.stringify({{
  note: parts[".sn-key-server-note"].textContent,
  noteClass: parts[".sn-key-server-note"].className,
  actionsHidden: parts[".sn-key-server-actions"].hidden,
  idle: String(parts[".sn-key-idle"].value),
  applyDisabled: parts[".sn-key-idle-apply"].disabled,
  stopDisabled: parts[".sn-key-stop-gpu"].disabled }}));
"""
        out = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_the_settings_fill_the_minutes_box(self):
        got = self._run(
            {"idle_timeout_minutes": 12, "default_idle_timeout_minutes": 30}
        )

        assert got["idle"] == "12"
        assert "12 min" in got["note"] and "30" in got["note"]
        assert not got["actionsHidden"]
        assert not got["applyDisabled"] and not got["stopDisabled"]

    def test_a_save_is_confirmed(self):
        got = self._run(
            {
                "idle_timeout_minutes": 5,
                "default_idle_timeout_minutes": 30,
                "saved": True,
            }
        )

        assert got["note"] == "Saved: models stop after 5 min idle"
        assert "sn-key-ok" in got["noteClass"]

    def test_a_server_without_remote_control_hides_the_controls_with_a_note(self):
        got = self._run({"unsupported": True})

        assert got["actionsHidden"]
        assert "does not support" in got["note"]
        assert "sn-err" not in got["noteClass"]

    def test_a_stop_says_the_gpu_is_free(self):
        got = self._run({"stopped": True})

        assert got["note"] == "Stopped — the GPU is free"
        assert "sn-key-ok" in got["noteClass"]
        assert not got["stopDisabled"]

    def test_an_error_is_shown_and_the_box_is_left_alone(self):
        got = self._run({"error": "Could not save: must be from 1 to 1440"}, idle="0")

        assert got["note"] == "Could not save: must be from 1 to 1440"
        assert "sn-err" in got["noteClass"]
        assert got["idle"] == "0"
        assert not got["applyDisabled"]
