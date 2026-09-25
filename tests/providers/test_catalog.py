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
    #: The provider whose model ids belong to its operator rather than to a vendor, so no
    #: curated list here could be right for anybody.
    OPERATOR_NAMED = {"openai_compatible"}

    def test_every_provider_answers_with_a_list(self):
        # Never a KeyError: the picker calls this for whatever provider is selected.
        for provider in LLM_PROVIDERS:
            assert isinstance(text_models(provider), list), provider

    def test_a_vendor_provider_offers_curated_ids(self):
        """A vendor's ids are knowable, so an empty list there means a provider was added to
        the picker and its models forgotten — which shows up as a dropdown with nothing in
        it."""
        for provider in LLM_PROVIDERS:
            if provider in self.OPERATOR_NAMED:
                continue
            assert text_models(provider), f"{provider} has no text models"

    def test_a_self_hosted_endpoint_offers_none_and_that_is_correct(self):
        """Its ids are whatever its operator named them — "omnia-local", a HuggingFace path.

        The user's own configured id is merged in by `catalog_payload`, which is the only list
        that can honestly be offered for such a provider.
        """
        assert text_models("openai_compatible") == []

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

    def test_piper_offers_a_vietnamese_voice(self):
        # piper curates a vi voice. Its weights are NOT packaged: the store resolves it from
        # user_files/models/piper -> models/piper -> a one-time download on first use.
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

    def test_fetched_voices_are_added_alongside_the_seed(self):
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
        # UNIONed, not replaced. This pinned the opposite until 2026-09-17: replacing meant a
        # cache that had only ever recorded a provider's own seed then shadowed that seed
        # forever, and the cache lives in the synced collection so it outlives any release.
        # See TestAStaleCacheCannotHideACuratedVoice.
        assert "edge_tts:vi-VN-HoaiMyNeural" in values


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


class TestAStaleCacheCannotHideACuratedVoice:
    """The Refresh cache is a UNION with the curated seed, not a replacement for it.

    It replaced per provider until 2026-09-17, on the assumption that a provider present in the
    cache had been fully enumerated. True of edge_tts (322 voices); false of the rest, because
    `refresh_voices` caches whatever a provider answered and a provider that cannot enumerate
    offline answers with its own curated seed. So the cache held a SNAPSHOT of the seed — and
    it lives in the SYNCED COLLECTION, so it outlives any release and shadowed the seed forever.

    That is not hypothetical. Seven en-US voices were added to `GoogleCloudTTS.CURATED_VOICES`;
    on a profile that had ever pressed Refresh they did not appear in the picker, and no amount
    of reinstalling or restarting helped — the code had them, the dropdown did not.
    """

    def _voice(self, provider, voice, **kw):
        from omnia.core.providers.tts import TTSVoice

        return TTSVoice(
            provider=provider,
            voice=voice,
            language=kw.get("language", "English (US)"),
            name=kw.get("name", voice),
            gender=kw.get("gender", "Male"),
            lang_code=kw.get("lang_code", "en"),
        )

    def _merged(self, fetched):
        from omnia.core.providers.catalog import _merged_voices

        return _merged_voices(fetched)

    def _curated(self, provider):
        from omnia.core.providers.catalog import aggregated_voices

        return {v.voice for v in aggregated_voices().get(provider, [])}

    def test_a_curated_voice_absent_from_the_cache_survives(self):
        curated = self._curated("google_cloud")
        assert curated, "google_cloud has no curated voices to test with"
        stale = [self._voice("google_cloud", sorted(curated)[0])]

        shown = {v.voice for v in self._merged({"google_cloud": stale})["google_cloud"]}

        assert (
            curated <= shown
        ), f"a stale cache hid {sorted(curated - shown)} — voices this build ships"

    def test_a_fetched_only_voice_is_added(self):
        # The reason the cache exists: edge_tts enumerates far more than its seed.
        shown = {
            v.voice
            for v in self._merged(
                {"google_cloud": [self._voice("google_cloud", "en-US-Fetched-Only")]}
            )["google_cloud"]
        }

        assert "en-US-Fetched-Only" in shown

    def test_a_fetched_entry_wins_on_its_own_id(self):
        # It carries the service's own metadata, which is better than the seed's guess.
        voice = sorted(self._curated("google_cloud"))[0]
        fetched = self._voice("google_cloud", voice, name="From The Service")

        merged = self._merged({"google_cloud": [fetched]})["google_cloud"]
        entry = next(v for v in merged if v.voice == voice)

        assert entry.name == "From The Service"

    def test_a_provider_absent_from_the_cache_keeps_its_seed(self):
        before = self._curated("edge_tts")

        shown = {v.voice for v in self._merged({"google_cloud": []})["edge_tts"]}

        assert shown == before

    def test_no_cache_at_all_is_the_bare_seed(self):
        assert self._merged(None)["google_cloud"]
        assert self._merged({})["google_cloud"]

    def test_the_payload_the_page_reads_shows_them_too(self):
        """End to end: `_merged_voices` being right is worth nothing if the payload re-derives."""
        from omnia.core.providers.catalog import catalog_payload

        curated = self._curated("google_cloud")
        stale = [self._voice("google_cloud", sorted(curated)[0])]

        payload = catalog_payload({"google_cloud": stale})
        shown = {v["voice"] for v in payload["voices"]["google_cloud"]}

        assert curated <= shown


class TestAConfiguredModelIsOfferedPerField:
    """A self-hosted endpoint has no curated ids, so without this the model the user already
    configured is the one thing the per-field picker cannot offer.

    Same shape as the voice-cache fix: merge what is known in, never replace what is shipped.
    """

    def _payload(self, text=None, image=None):
        from omnia.core.providers.catalog import catalog_payload

        return catalog_payload(None, text, image)

    def test_the_configured_id_appears(self):
        payload = self._payload({"openai_compatible": "omnia-local"})

        assert payload["text_models"]["openai_compatible"] == ["omnia-local"]

    def test_a_curated_list_keeps_its_order_and_gains_the_extra(self):
        """Curated order is a recommendation; the configured id is appended, not prepended."""
        curated = text_models("gemini")
        assert curated, "gemini lost its curated ids"

        merged = self._payload({"gemini": "some-private-preview"})["text_models"][
            "gemini"
        ]

        assert merged[: len(curated)] == curated
        assert merged[-1] == "some-private-preview"

    def test_an_id_already_curated_is_not_duplicated(self):
        curated = text_models("gemini")

        merged = self._payload({"gemini": curated[0]})["text_models"]["gemini"]

        assert merged == curated

    def test_an_unset_model_adds_nothing(self):
        for value in ("", "   ", None):
            payload = self._payload({"openai_compatible": value})

            assert payload["text_models"]["openai_compatible"] == []

    def test_image_models_merge_the_same_way(self):
        payload = self._payload(None, {"openai_compatible": "my-sdxl"})

        assert payload["image_models"]["openai_compatible"] == ["my-sdxl"]

    def test_no_configuration_at_all_is_the_curated_lists(self):
        payload = self._payload()

        assert payload["text_models"]["gemini"] == text_models("gemini")
        assert payload["text_models"]["openai_compatible"] == []
