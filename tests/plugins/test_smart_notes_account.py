"""Pure tests for the Smart Notes Account helpers + the OpenRouter credit fetch."""

from __future__ import annotations

from conftest import FakeHttpClient

from omnia.core.config.models import (
    EdgeTTSSettings,
    LLMSettings,
    OpenAICompatibleLLMSettings,
    TTSSettings,
)
from omnia.core.providers.llm.openai_compatible import OpenAICompatibleProvider
from omnia.plugins.smart_notes.account import (
    default_models,
    key_cards,
    merge_usage,
    models_in_use,
)
from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)


def _settings(fields=None) -> SmartNotesSettings:
    return SmartNotesSettings(
        note_types=[
            SmartNotesNoteTypeConfig(
                note_type="Vocab", base_field="Word", fields=fields or []
            )
        ]
    )


def _llm() -> LLMSettings:
    return LLMSettings(
        provider="openai",
        openai=OpenAICompatibleLLMSettings(
            api_key="k", text_model="gpt-text", image_model="gpt-image"
        ),
    )


class TestModelsInUse:
    def test_central_defaults_always_present(self):
        models = models_in_use(_settings(), _llm(), TTSSettings())
        assert {"provider": "openai", "model": "gpt-text"} in models["text"]
        assert {"provider": "openai", "model": "gpt-image"} in models["image"]
        assert {"provider": "google_translate", "model": "(default)"} in models["sound"]

    def test_field_override_provider_and_model(self):
        fields = [
            SmartNotesFieldConfig(
                field="Meaning",
                type="text",
                provider="gemini",
                model="gemini-2.0-flash",
            )
        ]
        models = models_in_use(_settings(fields), _llm(), TTSSettings())
        assert {"provider": "gemini", "model": "gemini-2.0-flash"} in models["text"]

    def test_blank_override_substitutes_central_default(self):
        fields = [SmartNotesFieldConfig(field="Meaning", type="text")]
        models = models_in_use(_settings(fields), _llm(), TTSSettings())
        # The field inherits the central provider+model; only one distinct text entry.
        assert models["text"] == [{"provider": "openai", "model": "gpt-text"}]

    def test_sound_uses_voice_as_model(self):
        fields = [
            SmartNotesFieldConfig(field="Audio", type="tts", voice="alloy"),
            SmartNotesFieldConfig(field="Audio2", type="tts"),  # blank voice
        ]
        models = models_in_use(_settings(fields), _llm(), TTSSettings())
        assert {"provider": "google_translate", "model": "alloy"} in models["sound"]
        assert {"provider": "google_translate", "model": "(default)"} in models["sound"]

    def test_distinct_pairs_are_deduped(self):
        fields = [
            SmartNotesFieldConfig(field="A", type="text"),
            SmartNotesFieldConfig(
                field="B", type="text"
            ),  # same inherited (provider,model)
        ]
        models = models_in_use(_settings(fields), _llm(), TTSSettings())
        assert len(models["text"]) == 1

    def test_image_field_uses_image_model(self):
        fields = [SmartNotesFieldConfig(field="Pic", type="image")]
        models = models_in_use(_settings(fields), _llm(), TTSSettings())
        assert models["image"] == [{"provider": "openai", "model": "gpt-image"}]


class TestMergeUsage:
    def test_left_join_attaches_counts(self):
        models = [{"provider": "openai", "model": "gpt-text"}]
        rows = [
            {
                "kind": "text",
                "provider": "openai",
                "model": "gpt-text",
                "calls": 3,
                "in_chars": 100,
                "out_chars": 50,
                "last_used_ts": 1234.0,
            }
        ]
        merged = merge_usage(models, rows, "text")
        assert merged[0]["calls"] == 3
        assert merged[0]["in_chars"] == 100
        assert merged[0]["out_chars"] == 50
        assert merged[0]["last_used_ts"] == 1234.0

    def test_unrecorded_model_gets_zero_counts(self):
        models = [{"provider": "openai", "model": "gpt-text"}]
        merged = merge_usage(models, [], "text")
        assert merged[0]["calls"] == 0
        assert merged[0]["last_used_ts"] is None

    def test_adhoc_usage_row_not_in_models_is_included(self):
        models = [{"provider": "openai", "model": "gpt-text"}]
        rows = [
            {
                "kind": "text",
                "provider": "gemini",
                "model": "extra",
                "calls": 1,
                "in_chars": 1,
                "out_chars": 1,
                "last_used_ts": 9.0,
            }
        ]
        merged = merge_usage(models, rows, "text")
        keys = {(r["provider"], r["model"]) for r in merged}
        assert ("openai", "gpt-text") in keys
        assert ("gemini", "extra") in keys

    def test_only_joins_matching_kind(self):
        models = [{"provider": "openai", "model": "m"}]
        rows = [
            {
                "kind": "sound",
                "provider": "openai",
                "model": "m",
                "calls": 5,
                "in_chars": 0,
                "out_chars": 0,
                "last_used_ts": None,
            }
        ]
        merged = merge_usage(models, rows, "text")
        # The sound row must not bleed into the text join.
        assert merged == [
            {
                "provider": "openai",
                "model": "m",
                "calls": 0,
                "in_chars": 0,
                "out_chars": 0,
                "in_tokens": 0,
                "out_tokens": 0,
                "last_used_ts": None,
            }
        ]


class TestDefaultModels:
    def test_text_and_image_read_active_llm_models(self):
        defaults = default_models(_llm(), TTSSettings())
        assert defaults["text"] == {"provider": "openai", "model": "gpt-text"}
        assert defaults["image"] == {"provider": "openai", "model": "gpt-image"}

    def test_sound_reads_active_tts_voice(self):
        tts = TTSSettings(provider="edge_tts", edge_tts=EdgeTTSSettings(voice="v-id"))
        defaults = default_models(_llm(), tts)
        assert defaults["sound"] == {"provider": "edge_tts", "model": "v-id"}

    def test_sound_voiceless_provider_has_blank_model(self):
        # google_translate has no voice field, so the sound default model is blank.
        defaults = default_models(_llm(), TTSSettings())
        assert defaults["sound"] == {"provider": "google_translate", "model": ""}


class TestKeyCards:
    def test_lists_managed_providers_in_order(self):
        cards = key_cards(_llm())
        assert [c["id"] for c in cards] == ["gemini", "gemini_vertex", "openrouter"]

    def test_only_openrouter_has_live_credit(self):
        by_id = {c["id"]: c for c in key_cards(_llm())}
        assert by_id["openrouter"]["credit"] == "live"
        assert by_id["gemini"]["credit"] == "note"
        assert by_id["gemini_vertex"]["credit"] == "note"

    def test_active_provider_is_flagged(self):
        llm = LLMSettings(
            provider="openrouter",
            openrouter=OpenAICompatibleLLMSettings(api_key="or-k"),
        )
        by_id = {c["id"]: c for c in key_cards(llm)}
        assert by_id["openrouter"]["active"] is True
        assert by_id["gemini"]["active"] is False

    def test_field_values_reflect_config(self):
        llm = LLMSettings(provider="gemini")
        llm.gemini.api_key = "secret-key"
        by_id = {c["id"]: c for c in key_cards(llm)}
        api_field = by_id["gemini"]["fields"][0]
        assert api_field == {
            "key": "api_key",
            "label": "API key",
            "type": "secret",
            "value": "secret-key",
            "placeholder": "",
        }

    def test_vertex_exposes_a_browseable_json_file_field(self):
        by_id = {c["id"]: c for c in key_cards(_llm())}
        file_fields = [
            f for f in by_id["gemini_vertex"]["fields"] if f["type"] == "file"
        ]
        assert len(file_fields) == 1
        assert file_fields[0]["key"] == "credentials_path"


class TestFetchCredit:
    def _provider(self, base_url: str, http) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(api_key="k", base_url=base_url, http=http)

    def test_openrouter_parses_remaining(self):
        http = FakeHttpClient(
            json={"data": {"total_credits": 10.0, "total_usage": 3.5}}
        )
        provider = self._provider("https://openrouter.ai/api/v1", http)
        credit = provider.fetch_credit()
        assert credit == {"total": 10.0, "used": 3.5, "remaining": 6.5}
        # It GETs the /credits endpoint with the api key.
        method, url, _params, headers = http.calls[0]
        assert method == "get_json"
        assert url.endswith("/credits")
        assert headers["Authorization"] == "Bearer k"

    def test_non_openrouter_returns_none(self):
        http = FakeHttpClient(json={"data": {"total_credits": 1, "total_usage": 0}})
        provider = self._provider("https://api.openai.com/v1", http)
        assert provider.fetch_credit() is None
        assert http.calls == []  # never hit the network for a non-OpenRouter endpoint

    def test_error_returns_none(self):
        def boom(*_a, **_k):
            raise KeyError("missing data")

        http = FakeHttpClient(responder=boom)
        provider = self._provider("https://openrouter.ai/api/v1", http)
        assert provider.fetch_credit() is None
