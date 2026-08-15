"""Offline guards for the provider registry's free-vs-requires-API classification.

The real-provider test files derive their ``llm``/``tts`` markers from this classification, so
it must (1) partition every provider into exactly one bucket and (2) match each provider
class's declared ``requires_api``. These run with no credentials and always pass — they keep
the classification honest as providers are added.

This file also freezes the two PUBLIC name lists (hand-written literals, not derived) and the
coupling between the registries and persisted config, so a refactor of the registry mechanism
cannot silently drop, rename, or orphan a provider name that users have in their config.
"""

from __future__ import annotations

from omnia.core.config.models import LLMSettings, TTSSettings
from omnia.core.providers import (
    available_keyless_llm_providers,
    available_keyless_tts_providers,
    available_llm_providers,
    available_llm_providers_requiring_api,
    available_tts_providers,
    available_tts_providers_requiring_api,
)
from omnia.core.providers.llm.openai_compatible import OpenAICompatibleProvider
from omnia.core.providers.llm.registry import LLM_REGISTRY
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.registry import TTS_REGISTRY

# Typed out BY HAND on purpose. Every other guard in this file is derived from the registry,
# so it would still pass if a name vanished — a tautology with respect to a registry refactor.
# These literals are the only thing that turns "a provider name disappeared or was renamed"
# into a failing diff. A name here is a persisted config value (``[llm.<name>]``, the synced
# ``SmartNotesFieldConfig.provider``, ``.secrets/llm.<name>.*``), so changing one is a
# migration, not an edit. Order is part of the contract: ``available_*`` is sorted, and
# conftest turns that order into pytest param IDs.
_LLM_NAMES = ["gemini", "gemini_vertex", "openai", "openai_compatible", "openrouter"]
_TTS_NAMES = [
    "edge_tts",
    "google_cloud",
    "google_translate",
    "openai",
    "openai_compatible",
    "openrouter",
    "piper",
    "viettts",
]


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
    def test_llm_lists_match_registry(self):
        # The available_* queries are derived straight from the registry's name set.
        assert set(available_llm_providers()) == set(LLM_REGISTRY)

    def test_llm_openai_family_shares_one_class(self):
        # openai/openrouter/openai_compatible are ONE class under three names — the invariant
        # ``smart_notes.account`` relies on to join usage rows onto the class name.
        for name in ("openai", "openrouter", "openai_compatible"):
            assert LLM_REGISTRY[name] is OpenAICompatibleProvider

    def test_tts_registry_lists_match_registry(self):
        # The available_* queries are derived straight from the registry's name set.
        assert set(available_tts_providers()) == set(TTS_REGISTRY)

    def test_tts_openai_family_shares_one_class(self):
        # openai/openrouter/openai_compatible are ONE class under three names.
        for name in ("openai", "openrouter", "openai_compatible"):
            assert TTS_REGISTRY[name] is OpenAICompatibleTTS

    def test_llm_requires_api_matches_classification(self):
        req = set(available_llm_providers_requiring_api())
        for name, cls in LLM_REGISTRY.items():
            assert (name in req) == cls.requires_api

    def test_tts_requires_api_matches_classification(self):
        req = set(available_tts_providers_requiring_api())
        for name, cls in TTS_REGISTRY.items():
            assert (name in req) == cls.requires_api


class TestPublicNameLists:
    """The registered names are a persisted contract, pinned against hand-written literals."""

    def test_llm_name_list_is_frozen(self):
        assert available_llm_providers() == _LLM_NAMES

    def test_tts_name_list_is_frozen(self):
        assert available_tts_providers() == _TTS_NAMES

    def test_every_registered_name_has_a_config_subsection(self):
        # Subset, not equality: a registered name with NO settings field is the bug — the hub
        # would hand the provider ``{"provider": name}`` with zero credentials and the user
        # would get a confusing auth error instead of a config error. An unused settings field
        # is merely dead config.
        assert set(available_llm_providers()) <= set(LLMSettings.__fields__)
        assert set(available_tts_providers()) <= set(TTSSettings.__fields__)
