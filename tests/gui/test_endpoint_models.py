"""Model discovery: an endpoint lists its models, and the Keys card offers them.

A self-hosted endpoint's model ids belong to its operator, so the user had to know and type
them. Omnia now asks the endpoint (`GET /models`, the OpenAI shape plus an optional `kind` per
model) and offers the answer in the Text model / Image model boxes, which stay editable for a
server that cannot list.
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
from omnia.core.providers.llm.openai_compatible import (
    ListedModel,
    OpenAICompatibleProvider,
)

# The Account controller's module imports Qt widgets; there is no display here.
install_gui_stubs()


class TestReadingAModelList:
    def test_each_model_keeps_its_kind(self):
        models = ListedModel.from_listing(
            {
                "data": [
                    {"id": "omnia-local", "kind": "text"},
                    {"id": "sdxl-turbo", "kind": "image"},
                ]
            }
        )
        assert models == [
            ListedModel("omnia-local", "text"),
            ListedModel("sdxl-turbo", "image"),
        ]

    def test_a_model_without_a_kind_is_a_text_model(self):
        """What every stock server (vLLM, Ollama, LM Studio) lists — none of them says a kind."""
        assert ListedModel.from_listing({"data": [{"id": "llama3"}]}) == [
            ListedModel("llama3")
        ]

    def test_an_unknown_kind_is_kept_as_text_rather_than_hidden(self):
        assert (
            ListedModel.from_listing({"data": [{"id": "x", "kind": "audio"}]})[0].kind
            == "text"
        )

    def test_blank_and_repeated_ids_are_dropped(self):
        models = ListedModel.from_listing(
            {"data": [{"id": ""}, {"id": "a"}, {"id": "a"}, "junk"]}
        )
        assert [m.id for m in models] == ["a"]

    def test_something_that_is_not_a_list_says_so(self):
        with pytest.raises(ProviderError):
            ListedModel.from_listing({"detail": "Not Found"})


class _Http:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get_json(self, url, *, params=None, headers=None):
        self.calls.append((url, headers or {}))
        return self.answer


class TestAskingTheEndpoint:
    def test_it_gets_models_under_the_base_url_with_the_key(self):
        http = _Http({"data": [{"id": "m", "kind": "image"}]})
        provider = OpenAICompatibleProvider(
            api_key="tok", base_url="https://h/v1/", http=http
        )

        assert provider.list_models() == [ListedModel("m", "image")]
        url, headers = http.calls[0]
        assert url == "https://h/v1/models"
        assert headers.get("Authorization") == "Bearer tok"


class TestTheKeysCardOp:
    """The controller op, with the background runner made synchronous."""

    def _controller(self, config_dir, monkeypatch, provider_answer):
        from omnia.core import anki_compat
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository
        from omnia.gui.smart_notes.dialogs.controllers.account import AccountController

        def run_now(
            fn, on_success=None, on_failure=None, label="", uses_collection=True
        ):
            calls.append({"uses_collection": uses_collection})
            try:
                result = fn()
            except Exception as exc:
                on_failure(exc)
            else:
                on_success(result)

        calls = []
        self.calls = calls
        monkeypatch.setattr(anki_compat, "run_in_background", run_now)
        seen = {}

        def list_models(self):
            seen["base_url"], seen["api_key"] = self._base_url, self._api_key
            seen["http"] = self._http
            if isinstance(provider_answer, Exception):
                raise provider_answer
            return provider_answer

        monkeypatch.setattr(OpenAICompatibleProvider, "list_models", list_models)
        evals = []
        ctx = types.SimpleNamespace(
            repo=ConfigRepository(ConfigLoader(config_dir)),
            eval_js=evals.append,
            friendly=lambda exc, prefix: f"{prefix}: {exc}",
        )
        return AccountController(ctx), evals, seen

    @staticmethod
    def _pushed(evals):
        match = re.fullmatch(r"window\.__snEndpointModels\((.*?), (.*)\);", evals[-1])
        return json.loads(match.group(1)), json.loads(match.group(2))

    def test_models_come_back_split_by_kind(self, config_dir, monkeypatch):
        answer = [ListedModel("omnia-local"), ListedModel("sdxl-turbo", "image")]
        controller, evals, _ = self._controller(config_dir, monkeypatch, answer)

        controller.on_list_endpoint_models(
            {"provider": "custom:gpu", "base_url": "https://h/v1", "api_key": "tok"}
        )

        assert self._pushed(evals) == (
            "custom:gpu",
            {"text": ["omnia-local"], "image": ["sdxl-turbo"]},
        )

    def test_what_is_typed_is_used_before_it_is_saved(self, config_dir, monkeypatch):
        controller, _evals, seen = self._controller(config_dir, monkeypatch, [])
        controller.on_list_endpoint_models(
            {
                "provider": "openai_compatible",
                "base_url": "https://typed/v1",
                "api_key": "typed",
            }
        )
        assert (seen["base_url"], seen["api_key"]) == ("https://typed/v1", "typed")

    def test_it_never_runs_on_the_collection_thread(self, config_dir, monkeypatch):
        """A GET that can hang to a socket timeout must not queue reviewing and syncing behind
        it, nor put Anki's modal progress over the whole app."""
        controller, _evals, _ = self._controller(config_dir, monkeypatch, [])

        controller.on_list_endpoint_models(
            {"provider": "p", "base_url": "https://h/v1", "api_key": "k"}
        )

        assert self.calls == [{"uses_collection": False}]

    def test_an_automatic_load_gives_up_fast_and_a_click_is_patient(
        self, config_dir, monkeypatch
    ):
        """Opening a tab must not wait out a dead tunnel; pressing the button may wait for a
        server that is waking up."""
        controller, _evals, seen = self._controller(config_dir, monkeypatch, [])

        controller.on_list_endpoint_models(
            {"provider": "p", "base_url": "https://h/v1", "api_key": "k", "auto": True}
        )
        quick = seen["http"]
        controller.on_list_endpoint_models(
            {"provider": "p", "base_url": "https://h/v1", "api_key": "k"}
        )
        patient = seen["http"]

        assert quick._timeout <= 10 and quick._retry.max_attempts == 1
        assert patient._timeout > quick._timeout

    def test_a_missing_url_is_said_plainly_and_nothing_is_asked(
        self, config_dir, monkeypatch
    ):
        controller, evals, seen = self._controller(config_dir, monkeypatch, [])

        controller.on_list_endpoint_models(
            {"provider": "custom:gpu", "base_url": "", "api_key": "t"}
        )

        assert "Base URL" in self._pushed(evals)[1]["error"]
        assert seen == {}

    def test_a_failure_carries_the_reason(self, config_dir, monkeypatch):
        controller, evals, _ = self._controller(
            config_dir,
            monkeypatch,
            ProviderError("HTTP 401: invalid or missing API key"),
        )

        controller.on_list_endpoint_models(
            {"provider": "custom:gpu", "base_url": "https://h/v1", "api_key": "bad"}
        )

        assert "401" in self._pushed(evals)[1]["error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
class TestTheCardOffersWhatWasListed:
    """The page half: the shipped receiver, run in node over a DOM just big enough."""

    def _run(self, typed_text="", typed_image="", payload=None):
        source = (
            Path(__file__).resolve().parents[2]
            / "src/omnia/gui/smart_notes/web/05-handlers.js"
        ).read_text()
        receiver = re.search(
            r"  window\.__snEndpointModels = function.*?\n  \};", source, re.S
        )
        assert receiver, "__snEndpointModels moved"
        script = f"""
function node(props) {{ return Object.assign({{children: [], className: "", textContent: "",
  appendChild(c) {{ this.children.push(c); }}, getAttribute(k) {{ return this.attrs[k]; }}, attrs: {{}} }}, props); }}
const lists = {{L1: node({{}}), L2: node({{}})}};
const text = node({{value: {json.dumps(typed_text)}, dataset: {{kind: "text"}}, attrs: {{list: "L1"}}}});
const image = node({{value: {json.dumps(typed_image)}, dataset: {{kind: "image"}}, attrs: {{list: "L2"}}}});
const note = node({{}});
const card = node({{dataset: {{provider: "custom:gpu"}},
  querySelector(s) {{ return s === ".sn-key-models-note" ? note : null; }},
  querySelectorAll(s) {{ return [text, image]; }} }});
const document = {{
  querySelectorAll() {{ return [card]; }},
  getElementById(id) {{ return lists[id]; }},
  createElement() {{ return node({{}}); }},
}};
const window = {{}};
{receiver.group(0).strip()}
window.__snEndpointModels("custom:gpu", {json.dumps(payload)});
console.log(JSON.stringify({{
  textOptions: lists.L1.children.map((o) => o.value), imageOptions: lists.L2.children.map((o) => o.value),
  text: text.value, image: image.value, note: note.textContent, noteClass: note.className }}));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_each_box_is_offered_the_models_of_its_kind(self):
        got = self._run(
            payload={"text": ["omnia-local", "qwen"], "image": ["sdxl-turbo"]}
        )

        assert got["textOptions"] == ["omnia-local", "qwen"]
        assert got["imageOptions"] == ["sdxl-turbo"]

    def test_an_empty_box_is_filled_with_the_first_model(self):
        got = self._run(payload={"text": ["omnia-local"], "image": ["sdxl-turbo"]})

        assert (got["text"], got["image"]) == ("omnia-local", "sdxl-turbo")

    def test_a_box_the_user_already_filled_is_left_alone(self):
        got = self._run(
            typed_text="my-model", payload={"text": ["omnia-local"], "image": []}
        )

        assert got["text"] == "my-model"

    def test_a_failure_says_so_and_leaves_typing_possible(self):
        got = self._run(payload={"error": "Could not list models: HTTP 404"})

        assert "404" in got["note"] and "type a model id" in got["note"]
        assert "sn-err" in got["noteClass"]
