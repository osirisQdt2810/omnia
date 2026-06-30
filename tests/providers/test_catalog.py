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
    voice_options_for_language,
    voices_for,
)
from omnia.core.providers.tts.base import TTSVoice


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
        assert providers_for("tts") == TTS_PROVIDERS

    def test_image_providers_exclude_non_image_capable(self):
        # Only providers that actually generate images are offered for image — openrouter
        # (no /images/generations endpoint) is excluded so it's never selectable + 404s.
        image = providers_for("image")
        assert "gemini" in image and "gemini_vertex" in image
        assert "openrouter" not in image


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

    def test_language_only_provider_has_no_named_voices(self):
        # google_translate is language-only — it enumerates no named voices.
        assert voices_for("google_translate") == []

    def test_piper_ships_a_bundled_vietnamese_voice(self):
        # piper now carries a bundled vi voice (resolved to models/piper/<name>.onnx).
        assert any(v.lang_code == "vi" for v in voices_for("piper"))


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

    def test_languages_expanded_to_broad_iso_set(self):
        langs = catalog_payload()["languages"]
        codes = {lang["code"] for lang in langs}
        # The "" Auto-detect entry was dropped (no per-field Language picker any more).
        assert "" not in codes
        # A broad ISO-639-1 set: spot-check a spread of the ~40 codes.
        assert {"vi", "en", "ja", "th", "ar", "uk", "te"}.issubset(codes)
        assert len(codes) >= 30


class TestTTSVoiceLangCode:
    def test_every_seed_voice_has_a_lang_code(self):
        for provider in ("edge_tts", "google_cloud", "viettts", "openai"):
            for voice in voices_for(provider):
                assert voice.lang_code, f"{provider}:{voice.voice} has no lang_code"

    def test_edge_tts_lang_codes_are_iso_639_1(self):
        codes = {v.lang_code for v in voices_for("edge_tts")}
        assert {"vi", "en", "ja", "th", "ar"}.issubset(codes)


class TestVoiceOptionsForLanguage:
    def test_includes_cross_provider_named_voices_for_vietnamese(self):
        options = voice_options_for_language("vi")
        # Every option is value="provider:voice" + a human label.
        for opt in options:
            assert ":" in opt["value"]
            assert opt["label"]
        providers = {opt["value"].split(":", 1)[0] for opt in options}
        # Cross-provider: edge_tts + viettts both serve Vietnamese, plus the free fallback.
        assert {"edge_tts", "viettts", "google_translate"}.issubset(providers)

    def test_language_only_provider_present_for_language_without_named_voice(self):
        # "th" (Thai) has an edge_tts seed voice but no viettts/openai voice — google_translate
        # must still be offered as the free, voiceless fallback.
        options = voice_options_for_language("th")
        values = {opt["value"] for opt in options}
        assert "google_translate:" in values

    def test_unknown_language_still_offers_the_free_fallback(self):
        # A code with no curated named voice at all still gets the google_translate option.
        options = voice_options_for_language("xx")
        assert any(opt["value"] == "google_translate:" for opt in options)

    def test_fetched_voices_replace_the_seed_for_their_provider(self):
        fetched = {
            "edge_tts": [
                TTSVoice(
                    "edge_tts",
                    "vi-VN-FetchedNeural",
                    "vi-VN",
                    "Fetched",
                    "Female",
                    "",
                    "vi",
                )
            ]
        }
        options = voice_options_for_language("vi", fetched)
        values = {opt["value"] for opt in options}
        assert "edge_tts:vi-VN-FetchedNeural" in values
        # The seed edge_tts voice is replaced (not merged) by the fetched list.
        assert "edge_tts:vi-VN-HoaiMyNeural" not in values


class TestAutoVoiceOptionsPayload:
    def test_keyed_by_every_non_empty_language(self):
        payload = catalog_payload()
        options = payload["auto_voice_options"]
        codes = {lang["code"] for lang in payload["languages"] if lang["code"]}
        assert set(options) == codes

    def test_auto_voice_options_are_json_serializable(self):
        import json

        json.dumps(catalog_payload()["auto_voice_options"])  # must not raise

    def test_fetched_voices_flow_into_the_payload(self):
        fetched = {
            "edge_tts": [
                TTSVoice(
                    "edge_tts",
                    "ja-JP-FetchedNeural",
                    "ja-JP",
                    "Fetched",
                    "Male",
                    "",
                    "ja",
                )
            ]
        }
        options = catalog_payload(fetched)["auto_voice_options"]["ja"]
        assert any(o["value"] == "edge_tts:ja-JP-FetchedNeural" for o in options)

    def test_fetched_only_provider_not_in_seed_reaches_the_payload(self):
        # A provider present only in the Refresh result (no curated seed entry) must still
        # contribute its voices to both the per-language options and the ``voices`` payload.
        fetched = {
            "azure_tts": [
                TTSVoice("azure_tts", "en-US-Foo", "en-US", "Foo", "Female", "", "en")
            ]
        }
        payload = catalog_payload(fetched)
        assert "azure_tts" in payload["voices"]
        options = payload["auto_voice_options"]["en"]
        assert any(o["value"] == "azure_tts:en-US-Foo" for o in options)
