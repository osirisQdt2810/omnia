"""Per-kind generation strategies for smart-notes field rules.

Each generation kind (text / image / tts) is one :class:`Generator` strategy, so the engine
dispatches on a rule's ``kind`` polymorphically instead of branching. Every generator is
constructed with the injected :class:`~omnia.core.providers.ProviderHub` (DIP), so the whole
engine unit-tests against a fake hub. Pure logic — no Anki imports at module top level.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from omnia.plugins.smart_notes.engine.language import LanguageDetector
from omnia.plugins.smart_notes.engine.markdown import convert_markdown_to_html
from omnia.plugins.smart_notes.engine.rules import prompt_for, tts_text

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule


@dataclass
class GenerationResult:
    """The output of one generation rule."""

    kind: str  # text | image | tts
    text: Optional[str] = None
    data: Optional[bytes] = None
    ext: str = ""


class Generator(ABC):
    """Produces the content for one generation rule against the configured providers."""

    @abstractmethod
    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        Raises:
            ProviderError: On bad config or a provider/network failure.
        """


class TextGenerator(Generator):
    """Generates Markdown→HTML text from the rule's interpolated prompt."""

    def __init__(self, providers: ProviderHub) -> None:
        self._providers = providers

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        llm = self._providers.llm(model=rule.model, provider=rule.provider)
        text = llm.generate_text(prompt_for(rule, fields))
        return GenerationResult("text", text=convert_markdown_to_html(text))


class ImageGenerator(Generator):
    """Generates a PNG image from the rule's interpolated prompt."""

    def __init__(self, providers: ProviderHub) -> None:
        self._providers = providers

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        # An image rule's model IS the image model — pin it as image_model so generate_image
        # targets it (pinning the text model would leave image_model unset).
        llm = self._providers.llm(image_model=rule.model, provider=rule.provider)
        data = llm.generate_image(prompt_for(rule, fields))
        return GenerationResult("image", data=data, ext="png")


class TTSGenerator(Generator):
    """Synthesizes audio from the rule's spoken text, resolving the voice two ways.

    The spoken text is the interpolated prompt (or the interpolated source field when no
    prompt is given). A rule with a concrete ``voice`` synthesizes it directly on the rule's
    provider (honoring the per-field provider). A rule on Auto-detect (no voice) detects the
    text's language and resolves a concrete ``(provider, voice)`` from the global
    ``[tts.auto_voices]`` map — so every language points at a provider+voice that serves it.
    """

    def __init__(self, providers: ProviderHub, detector: LanguageDetector) -> None:
        self._providers = providers
        self._detector = detector

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        text = tts_text(rule, fields)
        if rule.voice:
            # A pinned voice fixes the language; synthesize it on the rule's provider directly.
            provider = self._providers.tts(provider=rule.provider)
            data = provider.synthesize(text, lang=None, voice=rule.voice)
        else:
            # Auto-detect: find the language, then the global map's (provider, voice) for it.
            lang = rule.language or self._detector.detect(self._providers, text)
            picked_provider, voice = self._providers.resolve_auto_voice(lang or "")
            provider = self._providers.tts(provider=picked_provider)
            # An empty voice (a language-only provider, e.g. google_translate) → None so the
            # provider uses the language directly rather than an empty voice id.
            data = provider.synthesize(text, lang=lang, voice=voice or None)
        return GenerationResult("tts", data=data, ext=provider.audio_ext)
