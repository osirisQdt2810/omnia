"""Pure smart-notes logic: prompt interpolation + the provider-backed generation service.

No Anki imports. The :class:`GenerationService` depends on the injected
:class:`~omnia.core.providers.ProviderHub` (DIP), so it's tested with a fake hub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    from omnia.core.config.models import SmartNotesFieldRule
    from omnia.core.providers import ProviderHub

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)


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
