"""LAYER 2, provider side: ``generate_cached_text`` and the cached-token reporting.

Three things must hold, and each has a test here rather than a comment:

1. A provider that does nothing about caching sends the SAME BYTES it sent before — the base's
   default concatenates and delegates, so opting out is the safe direction.
2. A provider that reports how much of the input came from its cache surfaces that as
   ``usage["cached"]``, or prompt caching is a change with no evidence it did anything.
3. The one wire-format change (OpenRouter's ``cache_control`` marker) is off unless the config
   turns it on, because support is per MODEL and an unsupported model fails outright.
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import FakeHttpClient, FakeLLMProvider

from omnia.core.config.models import OpenAICompatibleLLMSettings
from omnia.core.providers.llm.base import LLMProvider, PromptParts
from omnia.core.providers.llm.gemini import GeminiProvider
from omnia.core.providers.llm.gemini_vertex import GeminiVertexProvider
from omnia.core.providers.llm.openai_compatible import OpenAICompatibleProvider

_GEMINI_TEXT = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
_OPENAI_TEXT = {"choices": [{"message": {"content": "ok"}}]}


def _payloads(http: FakeHttpClient) -> list[dict]:
    """The request bodies POSTed so far (auth/token round trips excluded)."""
    return [body for _m, url, body, _h in http.calls if "oauth2" not in url]


def _gemini(http: FakeHttpClient) -> GeminiProvider:
    return GeminiProvider(api_key="k", model="gemini-test", http=http)


def _openai(http: FakeHttpClient, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="k", model="gpt-test", http=http, **kwargs)


class TestPromptParts:
    def test_joined_is_exactly_the_two_parts_concatenated(self):
        assert PromptParts("Define ", "cat").joined() == "Define cat"

    def test_it_is_immutable(self):
        # Frozen on purpose: one PromptParts is shared by the caller, the wrapper and the
        # provider, and a mutation between them would change the prompt after the split was
        # proven lossless.
        parts = PromptParts("a", "b")
        with pytest.raises(dataclasses.FrozenInstanceError):
            parts.prefix = "c"  # type: ignore[misc]


class TestTheDefaultIsTodaysExactCall:
    def test_the_base_declares_no_prompt_cache_support(self):
        assert LLMProvider.supports_prompt_cache is False
        assert FakeLLMProvider(text="x").supports_prompt_cache is False

    def test_the_default_sends_the_concatenated_prompt_in_one_call(self):
        class _Spy(FakeLLMProvider):
            def __init__(self) -> None:
                super().__init__(text="out")
                self.prompts: list[str] = []

            def generate_text(
                self, prompt, *, system=None, temperature=0.7, max_tokens=None
            ):
                self.prompts.append(prompt)
                return self._text

        spy = _Spy()
        text, _usage = spy.generate_cached_text(PromptParts("Define ", "cat"))
        assert text == "out"
        assert spy.prompts == ["Define cat"]


class TestGeminiPromptCaching:
    def test_it_declares_support_and_vertex_inherits_it(self):
        # Gemini caches a repeated prefix implicitly, so the declaration covers both registered
        # names without either overriding anything.
        assert GeminiProvider.supports_prompt_cache is True
        assert GeminiVertexProvider.supports_prompt_cache is True

    def test_the_cached_call_puts_the_identical_payload_on_the_wire(self):
        # The byte-identical assertion: caching must not be able to change what is sent.
        http = FakeHttpClient(json=_GEMINI_TEXT)
        provider = _gemini(http)
        provider.generate_text("Define cat")
        provider.generate_cached_text(PromptParts("Define ", "cat"))
        uncached, cached = http.calls
        assert cached == uncached  # same method, URL, body AND headers

    def test_cached_tokens_are_reported_when_the_response_carries_them(self):
        http = FakeHttpClient(
            json={
                **_GEMINI_TEXT,
                "usageMetadata": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 40,
                    "totalTokenCount": 940,
                    "cachedContentTokenCount": 700,
                },
            }
        )
        _text, usage = _gemini(http).generate_cached_text(PromptParts("head ", "tail"))
        assert usage == {"in": 900, "out": 40, "total": 940, "cached": 700}

    def test_no_cached_key_when_the_response_reports_no_cache_hit(self):
        # Gemini omits the field on a miss; the usage dict must then look exactly as it always
        # did, so nothing downstream sees a new key it has to reason about.
        http = FakeHttpClient(
            json={
                **_GEMINI_TEXT,
                "usageMetadata": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 40,
                    "totalTokenCount": 940,
                },
            }
        )
        _text, usage = _gemini(http).generate_cached_text(PromptParts("head ", "tail"))
        assert usage == {"in": 900, "out": 40, "total": 940}


class TestOpenAICompatiblePromptCaching:
    def test_cache_control_is_off_by_default_and_sends_a_plain_string(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)
        _openai(http).generate_cached_text(PromptParts("Define ", "cat"))
        (payload,) = _payloads(http)
        assert payload["messages"] == [{"role": "user", "content": "Define cat"}]

    def test_off_by_default_means_byte_identical_to_an_uncached_call(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)
        provider = _openai(http)
        provider.generate_text("Define cat")
        provider.generate_cached_text(PromptParts("Define ", "cat"))
        uncached, cached = _payloads(http)
        assert cached == uncached

    def test_cache_control_on_marks_the_prefix_part_only(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)
        _openai(http, prompt_cache_control=True).generate_cached_text(
            PromptParts("Define ", "cat")
        )
        (payload,) = _payloads(http)
        assert payload["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Define ",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": "cat"},
                ],
            }
        ]

    def test_cache_control_on_still_sends_a_plain_string_with_no_prefix(self):
        # A template that leads with {{ref}} has nothing stable to mark. Marking the whole
        # prompt would cache one note's values — never a hit, and a wire change for nothing.
        http = FakeHttpClient(json=_OPENAI_TEXT)
        _openai(http, prompt_cache_control=True).generate_cached_text(
            PromptParts("", "cat — define it.")
        )
        (payload,) = _payloads(http)
        assert payload["messages"][0]["content"] == "cat — define it."

    def test_the_system_message_and_token_cap_survive_the_cached_path(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)
        _openai(http, prompt_cache_control=True).generate_cached_text(
            PromptParts("Define ", "cat"), system="Be terse.", max_tokens=64
        )
        (payload,) = _payloads(http)
        assert payload["messages"][0] == {"role": "system", "content": "Be terse."}
        assert payload["max_tokens"] == 64

    def test_cached_tokens_are_reported_when_the_response_carries_them(self):
        http = FakeHttpClient(
            json={
                **_OPENAI_TEXT,
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 40,
                    "total_tokens": 940,
                    "prompt_tokens_details": {"cached_tokens": 768},
                },
            }
        )
        _text, usage = _openai(http).generate_cached_text(PromptParts("head ", "tail"))
        assert usage == {"in": 900, "out": 40, "total": 940, "cached": 768}

    def test_a_zero_cached_count_is_not_reported(self):
        http = FakeHttpClient(
            json={
                **_OPENAI_TEXT,
                "usage": {
                    "prompt_tokens": 90,
                    "completion_tokens": 4,
                    "total_tokens": 94,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            }
        )
        _text, usage = _openai(http).generate_cached_text(PromptParts("head ", "tail"))
        assert usage == {"in": 90, "out": 4, "total": 94}


class TestTheSettingReachesTheProvider:
    def test_it_defaults_off_in_the_settings_model(self):
        assert OpenAICompatibleLLMSettings().prompt_cache_control is False

    def test_from_config_carries_it_through(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)
        config = {
            **OpenAICompatibleLLMSettings(prompt_cache_control=True).dict(),
            "provider": "openrouter",
            "api_key": "k",
            "model": "anthropic/claude-test",
        }
        OpenAICompatibleProvider.from_config(config, http).generate_cached_text(
            PromptParts("Define ", "cat")
        )
        (payload,) = _payloads(http)
        assert isinstance(payload["messages"][0]["content"], list)

    def test_an_absent_key_leaves_it_off(self):
        # A config written by an older device has no such key at all; the default must be the
        # safe direction (a plain string), never the wire change.
        http = FakeHttpClient(json=_OPENAI_TEXT)
        OpenAICompatibleProvider.from_config(
            {"provider": "openrouter", "api_key": "k", "model": "m"}, http
        ).generate_cached_text(PromptParts("Define ", "cat"))
        (payload,) = _payloads(http)
        assert payload["messages"][0]["content"] == "Define cat"


class TestJsonOutputMode:
    """LAYER 3's optional half: a schema the provider can enforce, that nothing depends on.

    The contract is asymmetric on purpose. A provider that CAN enforce the shape makes the
    caller's parse succeed more often; a provider that cannot must be indistinguishable from
    today, because the caller (K-note batching) parses defensively either way and its fallback
    ladder is what actually guarantees correctness.
    """

    _SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}

    def test_the_base_declares_no_json_support_and_sends_a_plain_text_call(self):
        assert LLMProvider.supports_json_output is False

        class _Recording(FakeLLMProvider):
            def __init__(self) -> None:
                super().__init__(text="{}")
                self.prompts: list[str] = []

            def generate_text(self, prompt, **kwargs):
                self.prompts.append(prompt)
                return self._text

        provider = _Recording()
        text, _usage = provider.generate_json(
            PromptParts("head ", "tail"), schema=self._SCHEMA
        )

        assert text == "{}"
        assert provider.prompts == ["head tail"]  # exactly the un-split prompt

    def test_gemini_asks_for_the_schema(self):
        http = FakeHttpClient(json=_GEMINI_TEXT)

        _gemini(http).generate_json(PromptParts("head ", "tail"), schema=self._SCHEMA)

        config = _payloads(http)[0]["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseSchema"] == self._SCHEMA
        # …and the prompt still arrives as one leading part, so implicit caching still applies.
        assert _payloads(http)[0]["contents"][0]["parts"][0]["text"] == "head tail"

    def test_vertex_inherits_the_same_json_wire_format(self):
        # One override, both registered names — the Vertex variant differs only in host + auth.
        assert GeminiVertexProvider.supports_json_output is True
        assert GeminiVertexProvider.generate_json is GeminiProvider.generate_json

    def test_openai_json_output_is_off_by_default_and_sends_no_response_format(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)

        _openai(http).generate_json(PromptParts("head ", "tail"), schema=self._SCHEMA)

        assert "response_format" not in _payloads(http)[0]

    def test_openai_json_output_on_sends_the_schema_envelope(self):
        http = FakeHttpClient(json=_OPENAI_TEXT)

        _openai(http, json_output=True).generate_json(
            PromptParts("head ", "tail"), schema=self._SCHEMA
        )

        fmt = _payloads(http)[0]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["schema"] == self._SCHEMA

    def test_json_output_composes_with_the_cache_control_marker(self):
        # Both opt-ins on: the content is still the two marked parts, AND the schema is asked
        # for. They ride the same request, so one must not quietly drop the other.
        http = FakeHttpClient(json=_OPENAI_TEXT)

        _openai(http, json_output=True, prompt_cache_control=True).generate_json(
            PromptParts("head ", "tail"), schema=self._SCHEMA
        )

        payload = _payloads(http)[0]
        content = payload["messages"][0]["content"]
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert payload["response_format"]["type"] == "json_schema"

    def test_the_settings_model_defaults_json_output_off(self):
        assert OpenAICompatibleLLMSettings().json_output is False

    def test_from_config_reads_the_flag(self):
        provider = OpenAICompatibleProvider.from_config(
            {"api_key": "k", "json_output": True}, http=FakeHttpClient(json={})
        )

        assert provider._json_output is True
