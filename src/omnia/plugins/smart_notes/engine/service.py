"""The provider-backed generation service that runs smart-notes field rules.

No Anki imports. :meth:`GenerationService.generate` runs one rule by dispatching to the
matching :class:`~omnia.plugins.smart_notes.engine.generators.Generator` strategy;
:meth:`GenerationService.generate_note` compiles a
:class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` into rules
(:func:`~omnia.plugins.smart_notes.engine.rules.compile_note_type_rules`) and runs them in
dependency order (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rules`), chaining
each text result into the field map so a downstream rule sees the freshly generated value.
The injected :class:`~omnia.core.providers.ProviderHub` keeps it testable with a fake hub (DIP).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.engine.generators import (
    GenerationResult,
    Generator,
    ImageGenerator,
    LanguageDetector,
    TextGenerator,
    TTSGenerator,
)
from omnia.plugins.smart_notes.engine.ordering import order_rules
from omnia.plugins.smart_notes.engine.rules import (
    compile_note_type_rules,
    should_skip_rule,
)

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )


class GenerationService:
    """Runs field-generation rules against the configured providers.

    :meth:`generate` runs one rule; :meth:`generate_note` compiles a
    :class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` into rules and runs them in
    dependency order, chaining each text result into the field map so a downstream rule sees
    the freshly generated value. Per-rule ``model``/``voice`` overrides layer on top of the
    central provider config.
    """

    def __init__(
        self, providers: ProviderHub, *, detect_tts_language: bool = True
    ) -> None:
        self._providers = providers
        # When a TTS rule pins no explicit voice, ask the LLM for the spoken text's language
        # so the voice matches it (a Vietnamese word shouldn't be read by an English voice).
        self._detect_tts_language = detect_tts_language
        self._generators: dict[str, Generator] = {
            "text": TextGenerator(providers),
            "image": ImageGenerator(providers),
            "tts": TTSGenerator(
                providers, LanguageDetector(enabled=detect_tts_language)
            ),
        }

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        Dispatches to the :class:`~omnia.plugins.smart_notes.engine.generators.Generator`
        strategy for ``rule.kind``. A per-rule ``provider``/``model`` selects a provider
        INSTANCE configured with that model (the model is fixed at construction, never threaded
        per call); for TTS the spoken text is the interpolated prompt (or the interpolated
        source field when no prompt is given) and ``voice`` overrides the configured voice.
        With no explicit voice, the spoken text's language is auto-detected so the provider
        picks a matching voice. Text results are rendered from Markdown to HTML for display in
        the card.

        Raises:
            ProviderError: On bad config, an unknown kind, or a provider/network failure.
        """
        generator = self._generators.get(rule.kind)
        if generator is None:
            raise ProviderError(f"Unknown generation kind: {rule.kind!r}")
        return generator.generate(rule, fields)

    def generate_note(
        self,
        config: SmartNotesNoteTypeConfig,
        fields: dict[str, str],
        *,
        allow_empty_fields: bool = False,
        force_overwrite: bool = False,
    ) -> list[tuple[SmartNotesFieldRule, GenerationResult]]:
        """Generate a note type's enabled fields, in dependency order, with chaining.

        The note type's generatable fields are compiled into rules
        (:func:`~omnia.plugins.smart_notes.engine.rules.compile_note_type_rules`),
        topologically ordered
        (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rules`) so a field that
        references another generated field runs after it, and each text result is written back
        into a working copy of ``fields`` so the dependent field interpolates the freshly
        generated value. The base field is never generated. Fields whose sources are all blank,
        or whose target is already filled, are skipped per
        :func:`~omnia.plugins.smart_notes.engine.rules.should_skip_rule`.

        Args:
            config: The note type's smart-notes config (its base field + per-field rows).
            fields: The note's current field values (not mutated).
            allow_empty_fields: Generate even when all referenced source fields are blank.
            force_overwrite: Regenerate every field even if its target is already non-empty
                (the batch "regenerate when batching" path), ignoring per-field ``overwrite``.

        Returns:
            ``(rule, result)`` pairs for the fields that actually generated, in run order.

        Raises:
            SmartNotesCycleError: If the fields reference each other in a cycle.
            ProviderError: On bad config or a provider/network failure.
        """
        rules = compile_note_type_rules(config)
        if force_overwrite:
            rules = [rule.copy(update={"overwrite": True}) for rule in rules]
        working = dict(fields)
        results: list[tuple[SmartNotesFieldRule, GenerationResult]] = []
        for rule in order_rules(rules):
            if should_skip_rule(rule, working, allow_empty_fields=allow_empty_fields):
                continue
            result = self.generate(rule, working)
            results.append((rule, result))
            # Only text feeds downstream prompts; media (image/tts) becomes an embed ref a
            # later prompt shouldn't consume.
            if result.kind == "text" and result.text is not None:
                working[rule.target_field] = result.text
        return results
