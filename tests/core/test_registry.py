"""Tests for the feature-plugin registry."""

from __future__ import annotations

import pytest

from omnia.core import registry
from omnia.core.plugin import FeaturePlugin


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolate each test from the global registry."""
    snapshot = dict(registry.FEATURE_REGISTRY)
    registry.FEATURE_REGISTRY.clear()
    yield
    registry.FEATURE_REGISTRY.clear()
    registry.FEATURE_REGISTRY.update(snapshot)


class TestRegistry:
    def test_register_stamps_id_and_records_class(self):
        @registry.register("demo")
        class Demo(FeaturePlugin):
            pass

        assert Demo.id == "demo"
        assert registry.get_registered()["demo"] is Demo

    def test_register_rejects_empty_id(self):
        with pytest.raises(ValueError):
            registry.register("")

    def test_register_rejects_duplicate_id(self):
        @registry.register("dup")
        class A(FeaturePlugin):
            pass

        with pytest.raises(ValueError):

            @registry.register("dup")
            class B(FeaturePlugin):
                pass

    def test_get_registered_returns_a_copy(self):
        @registry.register("x")
        class X(FeaturePlugin):
            pass

        snapshot = registry.get_registered()
        snapshot.clear()
        assert "x" in registry.FEATURE_REGISTRY
