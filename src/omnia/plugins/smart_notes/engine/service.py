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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnia.core.logging import get_logger
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
    rule_prerequisites,
    should_skip_rule,
)

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )

logger = get_logger("smart_notes")


@dataclass(frozen=True)
class BlockedField:
    """A field that was NOT generated because a HARD prerequisite was empty/failed.

    ``missing`` lists the prerequisite field names (display case) that were blank or had
    themselves been blocked/failed. Blocking is transitive: a blocked field puts no value in
    the working map, so its own hard dependents block in turn.
    """

    target_field: str
    missing: list[str]


@dataclass(frozen=True)
class FailedField:
    """A field whose generation raised (e.g. a provider/network error) and was isolated.

    Recording it (instead of letting the exception abort the whole note) lets sibling fields
    that already succeeded still be written; ``error`` is the exception's message for surfacing a
    count/diagnostic to the user. Like a blocked field, it produces no value, so its own hard
    dependents block transitively.
    """

    field: str
    error: str


def _hard_prerequisites(rule: SmartNotesFieldRule) -> list[str]:
    """Return the field names ``rule`` HARD-depends on (the gate's blocking prerequisites).

    Reads the rule's prerequisites through the single source of truth
    (:func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`) and keeps only the
    ``"hard"`` ones — soft prerequisites order generation but never block. The explicit
    kind-override (e.g. a derived source recoloured ``"soft"``) is already applied there, so a
    softened source is correctly excluded here. Names keep their original case (for the
    ``missing`` report); matching is the caller's job.
    """
    return [field for field, kind in rule_prerequisites(rule) if kind == "hard"]


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
    ) -> tuple[
        list[tuple[SmartNotesFieldRule, GenerationResult]],
        list[BlockedField],
        list[FailedField],
    ]:
        """Generate a note type's enabled fields, in dependency order, with chaining.

        The note type's generatable fields are compiled into rules
        (:func:`~omnia.plugins.smart_notes.engine.rules.compile_note_type_rules`),
        topologically ordered
        (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rules`) so a field that
        references another generated field runs after it, and each text result is written back
        into a working copy of ``fields`` so the dependent field interpolates the freshly
        generated value. The base field is never generated.

        Before each rule runs, its HARD prerequisites (derived prompt refs/source field, minus
        any the field marked ``"soft"`` in ``depends_on``, plus explicit hard deps) are checked
        against the working map: if any is blank or was itself blocked/failed, the rule is
        skipped and recorded as a :class:`BlockedField` (it writes no value, so its own hard
        dependents block transitively). Soft prerequisites never block. The existing skip
        predicate (:func:`~omnia.plugins.smart_notes.engine.rules.should_skip_rule`) still
        applies AFTER the block gate, so already-filled / all-sources-blank rules are skipped as
        before. A prerequisite that is "already filled and not overwritten" counts as present.

        Args:
            config: The note type's smart-notes config (its base field + per-field rows).
            fields: The note's current field values (not mutated).
            allow_empty_fields: Generate even when all referenced source fields are blank.
            force_overwrite: Regenerate every field even if its target is already non-empty
                (the batch "regenerate when batching" path), ignoring per-field ``overwrite``.

        A single field whose generation raises (e.g. a TTS field with no Auto-detect voice, or a
        provider/network error) is isolated: the exception is logged and recorded as a
        :class:`FailedField`, and generation continues with the remaining fields, so one
        misconfigured field never discards siblings that already succeeded. Like a blocked field,
        it writes no value, so its own hard dependents block transitively.

        Returns:
            A tuple ``(results, blocked, failed)`` where ``results`` is the ``(rule, result)``
            pairs for the fields that actually generated (in run order), ``blocked`` lists the
            fields skipped for a missing hard prerequisite, and ``failed`` lists the fields whose
            generation raised.

        Raises:
            SmartNotesCycleError: If the fields reference each other in a cycle. (A single field's
                provider/network failure is NOT raised — it is recorded in ``failed``.)
        """
        rules = compile_note_type_rules(config)
        if force_overwrite:
            rules = [rule.copy(update={"overwrite": True}) for rule in rules]
        working = dict(fields)
        results: list[tuple[SmartNotesFieldRule, GenerationResult]] = []
        blocked: list[BlockedField] = []
        failed: list[FailedField] = []
        # Lower-cased target names that generated a non-error result this run. Media (image/tts)
        # results are NOT chained into ``working`` (they are embed refs, not prompt text), so a
        # field hard-depending on a media field would falsely read it blank; ``produced`` records
        # the success so such a prerequisite still counts as satisfied.
        produced: set[str] = set()
        for rule in order_rules(rules):
            missing = self._missing_hard_prerequisites(rule, working, produced)
            if missing:
                blocked.append(BlockedField(rule.target_field, missing))
                continue  # writes no value → hard dependents block transitively
            if should_skip_rule(rule, working, allow_empty_fields=allow_empty_fields):
                continue
            try:
                result = self.generate(rule, working)
            except Exception as exc:  # one field's error must not abort the note
                logger.exception(
                    "smart_notes: field %r failed to generate", rule.target_field
                )
                failed.append(FailedField(rule.target_field, str(exc)))
                # Not added to results/produced/working, so its hard dependents block
                # transitively (same as a blocked field).
                continue
            results.append((rule, result))
            produced.add(rule.target_field.strip().lower())
            # Only text feeds downstream prompts; media (image/tts) becomes an embed ref a
            # later prompt shouldn't consume.
            if result.kind == "text" and result.text is not None:
                working[rule.target_field] = result.text
        return results, blocked, failed

    @staticmethod
    def _missing_hard_prerequisites(
        rule: SmartNotesFieldRule, working: dict[str, str], produced: set[str]
    ) -> list[str]:
        """Return the rule's hard prerequisites that are unmet (case-insensitive).

        A prerequisite is satisfied when it holds a non-blank value in ``working`` (an input
        field or a chained text result) OR its producing rule generated successfully this run
        (``produced`` — covers image/tts fields, whose embed refs are not chained into
        ``working``). It is "missing" only when it is genuinely blank AND was not produced — the
        case where it was itself blocked or its generation yielded an empty value, which
        propagates the block transitively. Returns the missing prerequisites' display names
        (empty list = all met).
        """
        present = {
            name.strip().lower()
            for name, value in working.items()
            if str(value).strip()
        }
        present |= produced
        return [
            prereq
            for prereq in _hard_prerequisites(rule)
            if prereq.strip().lower() not in present
        ]
