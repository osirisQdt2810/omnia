"""Tests for :func:`omnia.core.config.schema.schema_from_model`.

Locks the model → :class:`ConfigField` mapping the generic settings form relies on: type →
kind (bool/int/float/text/secret/choice), ``description`` → help, ``ge``/``le`` → bounds, the
field default, and that complex fields (typed lists/dicts, nested models) are skipped.
"""

from __future__ import annotations

import enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from omnia.core.config.schema import schema_from_model


class _Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class _Nested(BaseModel):
    x: int = 0


class _Sample(BaseModel):
    flag: bool = True
    count: int = Field(2, ge=0, le=10, description="how many")
    ratio: float = Field(0.5, ge=0.0, le=1.0)
    name: str = ""
    api_key: str = ""
    access_token: str = ""
    mode: Literal["a", "b"] = "a"
    color: _Color = _Color.RED
    # Complex fields — must be skipped by the deriver.
    mapping: dict[str, _Nested] = Field(default_factory=dict)
    items: list[_Nested] = Field(default_factory=list)
    nested: _Nested = Field(default_factory=_Nested)


def _by_key(model: type[BaseModel]) -> dict[str, object]:
    return {field.key: field for field in schema_from_model(model)}


class TestSchemaFromModel:
    def test_maps_scalar_kinds(self):
        fields = _by_key(_Sample)
        assert fields["flag"].kind == "bool"
        assert fields["count"].kind == "int"
        assert fields["ratio"].kind == "float"
        assert fields["name"].kind == "text"

    def test_str_named_like_credential_is_secret(self):
        fields = _by_key(_Sample)
        assert fields["api_key"].kind == "secret"
        assert fields["access_token"].kind == "secret"

    def test_literal_becomes_choice(self):
        fields = _by_key(_Sample)
        assert fields["mode"].kind == "choice"
        assert fields["mode"].choices == ("a", "b")

    def test_enum_becomes_choice(self):
        fields = _by_key(_Sample)
        assert fields["color"].kind == "choice"
        assert fields["color"].choices == ("red", "blue")

    def test_description_becomes_help(self):
        fields = _by_key(_Sample)
        assert fields["count"].help == "how many"
        assert fields["flag"].help == ""

    def test_ge_le_become_bounds(self):
        fields = _by_key(_Sample)
        assert fields["count"].minimum == 0
        assert fields["count"].maximum == 10
        assert fields["ratio"].minimum == 0.0
        assert fields["ratio"].maximum == 1.0

    def test_default_is_carried(self):
        fields = _by_key(_Sample)
        assert fields["flag"].default is True
        assert fields["count"].default == 2
        assert fields["mode"].default == "a"

    def test_complex_fields_are_skipped(self):
        keys = set(_by_key(_Sample))
        assert "mapping" not in keys  # typed dict
        assert "items" not in keys  # typed list
        assert "nested" not in keys  # nested model

    def test_order_follows_declaration(self):
        keys = [field.key for field in schema_from_model(_Sample)]
        assert keys == [
            "flag",
            "count",
            "ratio",
            "name",
            "api_key",
            "access_token",
            "mode",
            "color",
        ]


class _OnlyComplex(BaseModel):
    items: list = Field(default_factory=list)
    mapping: dict = Field(default_factory=dict)


class _OptionalScalar(BaseModel):
    note: Optional[int] = None


class TestSchemaEdgeCases:
    def test_bare_list_and_dict_are_skipped(self):
        assert schema_from_model(_OnlyComplex) == []

    def test_optional_scalar_still_rendered(self):
        fields = _by_key(_OptionalScalar)
        assert fields["note"].kind == "int"


class _ColorSample(BaseModel):
    text_color: str = "#c62828"
    color_token: str = ""  # 'token' is a secret hint AND 'color' — secret must win
    label: str = ""  # plain text (no colour/secret hint)


class TestColorDetection:
    def test_str_named_like_colour_is_color(self):
        assert _by_key(_ColorSample)["text_color"].kind == "color"

    def test_secret_hint_wins_over_colour(self):
        # A name matching both is a secret: the secret check runs before the colour check.
        assert _by_key(_ColorSample)["color_token"].kind == "secret"

    def test_plain_str_is_still_text(self):
        assert _by_key(_ColorSample)["label"].kind == "text"


class TestSchemaAgainstPluginModels:
    def test_typed_accuracy_pass_ease_is_choice(self):
        from omnia.plugins.typed_accuracy.config import TypedAccuracySettings

        fields = _by_key(TypedAccuracySettings)
        assert fields["pass_ease"].kind == "choice"
        assert fields["pass_ease"].choices == ("good", "easy", "no")
        assert fields["threshold"].minimum == 0.0
        assert fields["threshold"].maximum == 1.0

    def test_auto_flip_per_deck_is_skipped(self):
        from omnia.plugins.auto_flip.config import AutoFlipSettings

        keys = set(_by_key(AutoFlipSettings))
        assert "per_deck" not in keys
        assert {"delay_question_seconds", "wait_for_audio"} <= keys
