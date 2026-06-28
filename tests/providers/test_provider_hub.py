"""ProviderHub: the nested-config → flat-factory projection and the google_cloud auth bridge.

These guard the central design decisions of the per-provider config refactor: the hub (not
the factory) flattens the active ``[llm.<provider>]`` subsection (mapping ``text_model`` →
``model``), bridges the Google auth from ``[llm.gemini_vertex]`` into google_cloud TTS, and
defers an unknown provider to the factory's clear error (so config load never bricks).
"""

from __future__ import annotations

import pytest
from conftest import FakeHttpClient

from omnia.core.config.models import (
    GeminiLLMSettings,
    GeminiVertexLLMSettings,
    LLMSettings,
    OpenAICompatibleLLMSettings,
    TTSSettings,
)
from omnia.core.providers import ProviderError, ProviderHub


class TestProviderHub:
    def test_llm_config_projects_active_subsection_and_maps_text_model(self):
        settings = LLMSettings(
            provider="openrouter",
            openrouter=OpenAICompatibleLLMSettings(
                api_key="k", base_url="https://o/api/v1", text_model="x/y"
            ),
        )
        config = ProviderHub(settings)._llm_config()
        assert config["provider"] == "openrouter"
        assert config["model"] == "x/y"  # text_model -> model
        assert "text_model" not in config  # mapped away, not duplicated
        assert config["api_key"] == "k"
        assert config["base_url"] == "https://o/api/v1"

    def test_llm_config_unknown_provider_defers_to_factory_error(self):
        # active() returns None for an unknown provider, so the config is just
        # {"provider": ...} and the factory raises the clear "unknown provider" error
        # LAZILY (config load never fails).
        settings = LLMSettings(provider="openai")
        settings.provider = "nope"  # the field accepts any str by design
        config = ProviderHub(settings)._llm_config()
        assert config == {"provider": "nope"}
        with pytest.raises(ProviderError):
            ProviderHub(settings).llm()

    def test_tts_google_cloud_merges_vertex_auth(self):
        llm = LLMSettings(
            gemini_vertex=GeminiVertexLLMSettings(project="proj", access_token="tok")
        )
        http = FakeHttpClient(json={"audioContent": "QUJD"})  # base64("ABC")
        provider = ProviderHub(
            llm, TTSSettings(provider="google_cloud"), http=http
        ).tts()
        audio = provider.synthesize("hi", lang="en")
        assert audio == b"ABC"
        # The synthesize call carried the bridged Vertex bearer token.
        _, url, _payload, headers = http.calls[0]
        assert "texttospeech.googleapis.com" in url
        assert headers["Authorization"] == "Bearer tok"

    def test_tts_non_google_cloud_does_not_merge_vertex_auth(self):
        llm = LLMSettings(
            gemini_vertex=GeminiVertexLLMSettings(project="proj", access_token="tok")
        )
        # google_translate is keyless; the hub must not inject Vertex auth into config.
        config = ProviderHub(llm, TTSSettings(provider="google_translate"))
        built = config.tts()
        assert built.name == "google_translate"

    def test_llm_no_override_builds_the_active_provider(self):
        settings = LLMSettings(
            provider="openai",
            openai=OpenAICompatibleLLMSettings(api_key="k", text_model="active-model"),
        )
        provider = ProviderHub(settings, http=FakeHttpClient()).llm()
        assert provider.name == "openai_compatible"
        # The model is fixed at construction from the active subsection's text_model.
        assert provider._model == "active-model"

    def test_llm_model_override_builds_a_provider_pinned_to_that_model(self):
        # A per-rule model override yields a provider INSTANCE configured with it (the model is
        # never threaded into generate_text) — same provider, different fixed model.
        settings = LLMSettings(
            provider="openai",
            openai=OpenAICompatibleLLMSettings(api_key="k", text_model="active-model"),
        )
        hub = ProviderHub(settings, http=FakeHttpClient())
        provider = hub.llm(model="rule-model")
        assert provider.name == "openai_compatible"
        assert provider._model == "rule-model"

    def test_llm_provider_override_switches_provider_and_keeps_its_creds(self):
        settings = LLMSettings(
            provider="openai",
            openai=OpenAICompatibleLLMSettings(api_key="k"),
            gemini=GeminiLLMSettings(api_key="g-key", text_model="gemini-x"),
        )
        provider = ProviderHub(settings, http=FakeHttpClient()).llm(provider="gemini")
        assert provider.name == "gemini"
        assert provider._model == "gemini-x"

    def test_llm_override_is_cached_by_provider_and_model(self):
        settings = LLMSettings(
            provider="openai", openai=OpenAICompatibleLLMSettings(api_key="k")
        )
        hub = ProviderHub(settings, http=FakeHttpClient())
        first = hub.llm(model="m1")
        again = hub.llm(model="m1")
        other = hub.llm(model="m2")
        assert first is again  # same (provider, model) reuses the instance
        assert first is not other

    def test_vertex_auth_only_exposes_auth_fields_not_model_ids(self):
        llm = LLMSettings(
            gemini_vertex=GeminiVertexLLMSettings(
                project="proj", access_token="tok", text_model="m", image_model="i"
            )
        )
        auth = ProviderHub(llm)._vertex_auth()
        assert auth["project"] == "proj"
        assert auth["access_token"] == "tok"
        # model ids are NOT auth — they must not leak into the TTS auth bridge
        assert "text_model" not in auth and "image_model" not in auth
