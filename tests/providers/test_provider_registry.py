"""Tests for the generic provider registry (``core.providers.registry``).

Two halves. The mechanics half drives a THROWAWAY registry, so the registration rules are
covered without polluting ``LLM_REGISTRY``/``TTS_REGISTRY`` — which the sweep's set-equality
guards would then fail. That isolation is the concrete payoff of the registry being an object
rather than a module-level dict. The conformance half is read-only against both real
registries and is what proves one mechanism now behaves identically for LLM and TTS.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from omnia.core.providers.base import ProviderBase
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.llm.registry import LLM_REGISTRY
from omnia.core.providers.registry import ProviderRegistry
from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.registry import TTS_REGISTRY


class _DummyProvider(ProviderBase):
    """A minimal provider: records the config it was built from."""

    name = "dummy"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[Any] = None
    ) -> _DummyProvider:
        return cls(config)


class _KeylessDummy(_DummyProvider):
    """A second class, keyless — for the partition and duplicate-name rules."""

    name = "keyless_dummy"
    requires_api = False


class TestProviderRegistryMechanics:
    """Registration/creation rules, exercised on a registry the test owns."""

    @pytest.fixture
    def registry(self) -> ProviderRegistry[_DummyProvider]:
        reg: ProviderRegistry[_DummyProvider] = ProviderRegistry(
            "DUMMY", default="dummy"
        )
        reg.register("dummy", "dummy_alias")(_DummyProvider)
        return reg

    def test_no_names_raises(self, registry):
        with pytest.raises(ValueError):
            registry.register()

    def test_empty_name_raises(self, registry):
        with pytest.raises(ValueError):
            registry.register("")(_KeylessDummy)

    def test_same_class_reregister_is_noop(self, registry):
        before = dict(registry)
        registry.register("dummy")(_DummyProvider)
        assert before == registry

    def test_duplicate_name_different_class_raises(self, registry):
        with pytest.raises(ValueError):
            registry.register("dummy")(_KeylessDummy)

    def test_one_class_under_several_names_appears_once_in_classes(self, registry):
        assert registry.classes() == [_DummyProvider]
        assert registry.names() == ["dummy", "dummy_alias"]

    def test_names_are_sorted(self, registry):
        # Sorted, not registration order: conftest turns this list into pytest param IDs.
        registry.register("aaa")(_KeylessDummy)
        assert registry.names() == sorted(registry.names())

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("does-not-exist") is None

    def test_buckets_partition_the_names(self, registry):
        registry.register("free")(_KeylessDummy)
        assert registry.requiring_api() == ["dummy", "dummy_alias"]
        assert registry.keyless() == ["free"]

    def test_create_uses_the_kinds_default_when_config_names_none(self, registry):
        built = registry.create({})
        assert isinstance(built, _DummyProvider)

    def test_create_passes_the_whole_config_through(self, registry):
        # The provider key must reach from_config: a multi-name class reads it to pick its own
        # defaults (which base URL the openai family points at).
        built = registry.create({"provider": "dummy_alias", "api_key": "k"})
        assert built.config == {"provider": "dummy_alias", "api_key": "k"}

    def test_create_unknown_provider_raises_with_the_known_names(self, registry):
        with pytest.raises(ProviderError) as exc:
            registry.create({"provider": "nope"})
        assert "DUMMY" in str(exc.value)
        assert "dummy_alias" in str(exc.value)

    def test_register_does_not_stamp_the_class_name(self, registry):
        # A multi-name class keeps its own declared name — the usage rows and the Account tab
        # join on it, so stamping the registry key would break both.
        assert registry["dummy_alias"].name == "dummy"


@pytest.mark.parametrize(
    ("registry", "base"),
    [(LLM_REGISTRY, LLMProvider), (TTS_REGISTRY, TTSProvider)],
    ids=["llm", "tts"],
)
class TestBothKindsConformToTheRegistry:
    """The same guarantees must hold for every kind bound onto the generic registry."""

    def test_every_name_resolves_to_a_provider_subclass(self, registry, base):
        assert registry
        for cls in registry.values():
            assert issubclass(cls, base)

    def test_names_are_sorted_and_match_the_mapping(self, registry, base):
        assert registry.names() == sorted(registry.names())
        assert set(registry.names()) == set(registry)

    def test_api_buckets_partition_the_names(self, registry, base):
        req = set(registry.requiring_api())
        free = set(registry.keyless())
        assert req | free == set(registry.names())
        assert req.isdisjoint(free)

    def test_every_class_defines_its_own_from_config(self, registry, base):
        # Inherited is not enough: GeminiVertexProvider subclasses GeminiProvider, so an
        # inherited from_config would reject a valid Vertex project with "requires an api_key".
        for cls in registry.classes():
            assert "from_config" in cls.__dict__, f"{cls.__name__} inherits from_config"

    def test_rebinding_a_live_name_to_another_class_raises(self, registry, base):
        class _Intruder(base):  # type: ignore[misc,valid-type]
            pass

        with pytest.raises(ValueError):
            registry.register(next(iter(registry)))(_Intruder)
