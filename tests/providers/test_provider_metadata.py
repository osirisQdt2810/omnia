"""Offline guards for the provider registry's free-vs-requires-API classification.

The real-provider test files derive their ``llm``/``tts`` markers from this classification, so
it must (1) partition every provider into exactly one bucket and (2) match each provider
class's declared ``requires_api``. These run with no credentials and always pass — they keep
the classification honest as providers are added.
"""

from __future__ import annotations

from omnia.core.providers import (
    available_keyless_llm_providers,
    available_keyless_tts_providers,
    available_llm_providers,
    available_llm_providers_requiring_api,
    available_tts_providers,
    available_tts_providers_requiring_api,
)
from omnia.core.providers.llm.factory import _BUILDERS as LLM_BUILDERS
from omnia.core.providers.llm.factory import _PROVIDER_CLASSES as LLM_CLASSES
from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.registry import TTS_REGISTRY


class TestProviderClassification:
    def test_llm_buckets_partition_all_providers(self):
        allp = set(available_llm_providers())
        req = set(available_llm_providers_requiring_api())
        free = set(available_keyless_llm_providers())
        assert req | free == allp
        assert req.isdisjoint(free)

    def test_tts_buckets_partition_all_providers(self):
        allp = set(available_tts_providers())
        req = set(available_tts_providers_requiring_api())
        free = set(available_keyless_tts_providers())
        assert req | free == allp
        assert req.isdisjoint(free)

    def test_all_llm_providers_currently_require_api(self):
        # No keyless/offline LLM provider exists yet; adding one (e.g. local llama) flips this.
        assert available_keyless_llm_providers() == []

    def test_keyless_tts_are_the_free_offline_ones(self):
        # Free/offline/local-open-source providers need no cloud key: google_translate + Edge
        # (both pure-stdlib clients), piper (local model), viettts (local self-hosted server).
        assert set(available_keyless_tts_providers()) == {
            "google_translate",
            "edge_tts",
            "piper",
            "viettts",
        }


class TestClassMetadataConsistency:
    def test_llm_classes_cover_builders(self):
        # Drift guard: the name->class map used for classification must match the builders.
        assert set(LLM_CLASSES) == set(LLM_BUILDERS)

    def test_tts_registry_resolves_to_provider_subclasses(self):
        # Every registered name resolves to a TTSProvider subclass.
        assert TTS_REGISTRY
        for cls in TTS_REGISTRY.values():
            assert issubclass(cls, TTSProvider)

    def test_tts_registry_lists_match_registry(self):
        # The available_* queries are derived straight from the registry's name set.
        assert set(available_tts_providers()) == set(TTS_REGISTRY)

    def test_tts_openai_family_shares_one_class(self):
        # openai/openrouter/openai_compatible are ONE class under three names.
        for name in ("openai", "openrouter", "openai_compatible"):
            assert TTS_REGISTRY[name] is OpenAICompatibleTTS

    def test_llm_requires_api_matches_classification(self):
        req = set(available_llm_providers_requiring_api())
        for name, cls in LLM_CLASSES.items():
            assert (name in req) == cls.requires_api

    def test_tts_requires_api_matches_classification(self):
        req = set(available_tts_providers_requiring_api())
        for name, cls in TTS_REGISTRY.items():
            assert (name in req) == cls.requires_api
