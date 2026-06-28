"""Pure smart-notes logic: prompt interpolation + the provider-backed generation service.

No Anki imports. The :class:`GenerationService` depends on the injected
:class:`~omnia.core.providers.ProviderHub` (DIP), so it's tested with a fake hub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    from omnia.core.config.models import SmartNotesFieldRule
    from omnia.core.providers import ProviderHub

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")

# The valid generation kinds; rows with anything else collapse to the default.
_VALID_KINDS = ("text", "image", "tts")
# The fields the dialog rows carry, in column order.
_ROW_KEYS = ("note_type", "source_field", "target_field", "kind", "prompt")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)


def rules_to_rows(rules: list[SmartNotesFieldRule]) -> list[dict[str, str]]:
    """Project field rules into plain dict rows for the field-mapping dialog table."""
    return [{key: getattr(rule, key) for key in _ROW_KEYS} for rule in rules]


def rows_to_rules(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalise dialog rows into rule dicts, dropping blank rows.

    Whitespace is stripped from each cell; a row is dropped if every cell is empty after
    stripping. An unrecognised ``kind`` falls back to ``"text"`` so a stray combo value can't
    fail validation; real validation happens when the dialog builds ``SmartNotesSettings``.
    """
    cleaned: list[dict[str, str]] = []
    for row in rows:
        values = {key: str(row.get(key, "") or "").strip() for key in _ROW_KEYS}
        if not any(values.values()):
            continue  # a fully blank row is the table's "add another" affordance
        if values["kind"] not in _VALID_KINDS:
            values["kind"] = "text"
        cleaned.append(values)
    return cleaned


def build_generation_plan(
    fields: dict[str, str], note_type: str, rules: list[SmartNotesFieldRule]
) -> list[tuple[SmartNotesFieldRule, dict[str, str]]]:
    """Select the rules that apply to one note's ``fields`` of type ``note_type``.

    A rule applies when its (optional) ``note_type`` matches and its ``target_field`` exists
    on the note. Pure so both the Browser menu and the editor button share the same selection
    logic; each pairs the rule with the note's current field values for generation.
    """
    return [
        (rule, fields)
        for rule in rules
        if (not rule.note_type or rule.note_type == note_type)
        and rule.target_field in fields
    ]


@dataclass
class GenerationResult:
    """The output of one generation rule."""

    kind: str  # text | image | tts
    text: Optional[str] = None
    data: Optional[bytes] = None
    ext: str = ""


class GenerationService:
    """Runs a single field-generation rule against the configured providers."""

    def __init__(self, providers: ProviderHub) -> None:
        self._providers = providers

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        Raises:
            ProviderError: On bad config or a provider/network failure.
        """
        if rule.kind == "text":
            text = self._providers.llm().generate_text(self._prompt(rule, fields))
            return GenerationResult("text", text=text)
        if rule.kind == "image":
            data = self._providers.llm().generate_image(self._prompt(rule, fields))
            return GenerationResult("image", data=data, ext="png")
        if rule.kind == "tts":
            provider = self._providers.tts()
            source = fields.get(rule.source_field, "")
            data = provider.synthesize(source)
            return GenerationResult("tts", data=data, ext=provider.audio_ext)
        raise ProviderError(f"Unknown generation kind: {rule.kind!r}")

    @staticmethod
    def _prompt(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
        """The prompt for a text/image rule: the template if given, else the source field."""
        if rule.prompt:
            return interpolate(rule.prompt, fields)
        return fields.get(rule.source_field, "")
