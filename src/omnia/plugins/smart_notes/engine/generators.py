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
    from omnia.core.providers.tts import TTSProvider
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule


@dataclass
class GenerationResult:
    """The output of one generation rule."""

    kind: str  # text | image | tts
    text: Optional[str] = None
    data: Optional[bytes] = None
    ext: str = ""
    # Which tool produced this, stamped by the pipeline on the winning attempt (a generator
    # itself never sets it). Provenance only — nothing about the CONTENT depends on it — but it
    # is what lets the batch summary count the fields a chain had to fall back on, so a
    # deterministic first tool that quietly stops matching is visible instead of just expensive.
    tool: str = ""


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


@dataclass(frozen=True)
class ResolvedVoice:
    """One rule's concrete TTS provider + voice + language, ready to speak with.

    Resolving a voice is a two-branch decision (a pinned voice, or the Auto-detect map keyed by
    the detected language) that every synthesis of that rule must make the SAME way. It became
    a value object once a second caller appeared: ``cloze_audio`` synthesizes a sentence in
    several pieces and must splice them, which only works if every piece came from one provider
    at one voice — so it needs to resolve ONCE and reuse, not re-decide per call.
    """

    provider: TTSProvider
    lang: Optional[str]
    voice: Optional[str]

    @classmethod
    def for_rule(
        cls,
        providers: ProviderHub,
        detector: LanguageDetector,
        rule: SmartNotesFieldRule,
        text: str,
    ) -> ResolvedVoice:
        """Resolve the voice ``rule`` should be spoken with.

        Args:
            providers: The hub that builds the configured providers.
            detector: The best-effort language detector (used only on the Auto-detect branch).
            rule: The rule being generated (its ``voice``/``provider``/``language`` overrides).
            text: The text whose language is detected when the rule pins no voice. Nothing is
                synthesized here, so a caller that must not leak part of its text (audio cloze)
                may pass a redacted sample.

        Returns:
            The resolved provider/voice/language.

        Raises:
            ProviderError: When Auto-detect has no voice mapped for the detected language.
        """
        if rule.voice:
            # A pinned voice fixes the language; synthesize on the rule's provider directly.
            return cls(providers.tts(provider=rule.provider), None, rule.voice)
        # Auto-detect: find the language, then the global map's (provider, voice) for it.
        lang = rule.language or detector.detect(providers, text)
        picked_provider, voice = providers.resolve_auto_voice(lang or "")
        # An empty voice (a language-only provider, e.g. google_translate) → None so the
        # provider uses the language directly rather than an empty voice id.
        return cls(providers.tts(provider=picked_provider), lang, voice or None)

    @property
    def audio_ext(self) -> str:
        """The container/extension of the bytes :meth:`synthesize` returns (``wav``/``mp3``)."""
        return self.provider.audio_ext

    def synthesize(self, text: str) -> bytes:
        """Speak ``text`` with this resolved provider/voice/language."""
        return self.provider.synthesize(text, lang=self.lang, voice=self.voice)


class TTSGenerator(Generator):
    """Synthesizes audio from the rule's spoken text, resolving the voice two ways.

    The spoken text is the interpolated prompt (or the interpolated source field when no
    prompt is given); :class:`ResolvedVoice` owns the pinned-voice vs Auto-detect decision.
    """

    def __init__(self, providers: ProviderHub, detector: LanguageDetector) -> None:
        self._providers = providers
        self._detector = detector

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        text = tts_text(rule, fields)
        resolved = ResolvedVoice.for_rule(self._providers, self._detector, rule, text)
        return GenerationResult(
            "tts", data=resolved.synthesize(text), ext=resolved.audio_ext
        )
