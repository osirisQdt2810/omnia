"""Tests for the provider/model/voice catalog that drives the Smart Notes dropdowns.

Pure data + helpers — no provider is built and no network is touched.
"""

from __future__ import annotations

from omnia.core.providers import available_llm_providers, available_tts_providers
from omnia.core.providers.catalog import (
    LLM_PROVIDERS,
    TTS_PROVIDERS,
    catalog_payload,
    image_models,
    models_for,
    providers_for,
    text_models,
    voices_for,
)


class TestProviderSubsets:
    def test_llm_subset_is_a_subset_of_registered_providers(self):
        # The generation picker omits raw openai/openai_compatible (openrouter fronts them).
        assert set(LLM_PROVIDERS).issubset(set(available_llm_providers()))
        assert "openai" not in LLM_PROVIDERS
        assert "gemini" in LLM_PROVIDERS and "openrouter" in LLM_PROVIDERS

    def test_tts_subset_is_a_subset_of_registered_providers(self):
        assert set(TTS_PROVIDERS).issubset(set(available_tts_providers()))
        assert "edge_tts" in TTS_PROVIDERS

    def test_providers_for_kind_routes_llm_vs_tts(self):
        assert providers_for("text") == LLM_PROVIDERS
        assert providers_for("image") == LLM_PROVIDERS
        assert providers_for("tts") == TTS_PROVIDERS


class TestModels:
    def test_text_models_present_for_each_llm_provider(self):
        for provider in LLM_PROVIDERS:
            assert text_models(provider), f"{provider} has no text models"

    def test_models_for_image_kind_uses_image_list(self):
        assert models_for("gemini", "image") == image_models("gemini")
        assert models_for("gemini", "text") == text_models("gemini")

    def test_unknown_provider_yields_empty(self):
        assert text_models("nope") == []
        assert image_models("nope") == []

    def test_returned_lists_are_copies(self):
        a = text_models("gemini")
        a.append("mutated")
        assert "mutated" not in text_models("gemini")


class TestVoices:
    def test_edge_has_vietnamese_and_english_voices(self):
        langs = {v.language for v in voices_for("edge_tts")}
        assert "Vietnamese" in langs
        assert any(lang.startswith("English") for lang in langs)

    def test_voice_label_is_language_name_gender(self):
        voice = voices_for("edge_tts")[0]
        assert voice.label == f"{voice.language} · {voice.name} · {voice.gender}"

    def test_provider_without_offline_voices_is_empty(self):
        # google_translate is language-only; piper is a local .onnx path — neither enumerates.
        assert voices_for("google_translate") == []
        assert voices_for("piper") == []


class TestCatalogPayload:
    def test_payload_shape(self):
        payload = catalog_payload()
        assert payload["llm_providers"] == LLM_PROVIDERS
        assert payload["tts_providers"] == TTS_PROVIDERS
        assert set(payload["text_models"]) == set(LLM_PROVIDERS)
        assert set(payload["image_models"]) == set(LLM_PROVIDERS)

    def test_payload_voices_are_jsonable_dicts(self):
        payload = catalog_payload()
        entry = payload["voices"]["edge_tts"][0]
        assert set(entry) == {"voice", "label", "language", "gender", "model"}
        assert isinstance(entry["voice"], str) and entry["voice"]

    def test_payload_is_json_serializable(self):
        import json

        json.dumps(catalog_payload())  # must not raise

    def test_languages_present_with_auto_detect_first(self):
        langs = catalog_payload()["languages"]
        assert langs[0]["code"] == "" and "auto" in langs[0]["label"].lower()
        codes = {lang["code"] for lang in langs}
        assert "vi" in codes and "en" in codes
