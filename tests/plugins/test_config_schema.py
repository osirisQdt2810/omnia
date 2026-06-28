"""Each feature's config_schema() must map to real keys on its settings model.

This locks the GUI contract: the generic settings form writes ``{field.key: value}`` under
the plugin's config section, so every declared field must exist on the typed model.
"""

from __future__ import annotations

import pytest

from omnia.core.config.models import (
    AutoFlipSettings,
    OverdueGuardSettings,
    TypedAccuracySettings,
)
from omnia.core.plugin import FIELD_KINDS
from omnia.plugins.auto_flip import AutoFlipPlugin
from omnia.plugins.overdue_guard import OverdueGuardPlugin
from omnia.plugins.typed_accuracy import TypedAccuracyPlugin

_CASES = [
    (AutoFlipPlugin, AutoFlipSettings),
    (TypedAccuracyPlugin, OverdueGuardSettings if False else TypedAccuracySettings),
    (OverdueGuardPlugin, OverdueGuardSettings),
]


class TestConfigSchema:
    @pytest.mark.parametrize("plugin_cls,settings_cls", _CASES)
    def test_config_schema_keys_exist_on_settings(self, plugin_cls, settings_cls):
        schema = plugin_cls().config_schema()
        assert schema, f"{plugin_cls.__name__} should declare config fields"
        model_fields = set(settings_cls.__fields__)
        for field in schema:
            assert (
                field.key in model_fields
            ), f"{field.key} not on {settings_cls.__name__}"
            assert field.kind in FIELD_KINDS

    @pytest.mark.parametrize("plugin_cls,settings_cls", _CASES)
    def test_config_schema_defaults_validate(self, plugin_cls, settings_cls):
        # Building the settings model from the schema defaults must pass validation.
        defaults = {f.key: f.default for f in plugin_cls().config_schema()}
        settings_cls(**defaults)
