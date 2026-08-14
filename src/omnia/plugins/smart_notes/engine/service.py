"""The provider-backed generation service that runs smart-notes field rules.

No Anki imports. :meth:`GenerationService.generate` runs one rule by handing it to the
:class:`~omnia.plugins.smart_notes.engine.tools.pipeline.GenerationPipeline`, which tries the
rule's ordered tool chain until one produces (a field with no configured chain compiles to the
single ``"ai"`` tool — the provider-backed path — so behaviour is unchanged);
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
from omnia.plugins.smart_notes.engine.generators import LanguageDetector
from omnia.plugins.smart_notes.engine.ordering import order_rules
from omnia.plugins.smart_notes.engine.rules import (
    compile_note_type_rules,
    rule_prerequisites,
    should_skip_rule,
)
from omnia.plugins.smart_notes.engine.tools import (
    GenerationPipeline,
    ToolChainError,
    ToolContext,
)

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )
    from omnia.plugins.smart_notes.engine.generators import GenerationResult

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
    """A field whose whole tool chain ran without producing anything, and was isolated.

    Recording it (instead of letting the exception abort the whole note) lets sibling fields
    that already succeeded still be written; ``error`` is the chain's attempt summary — for the
    legacy single-``ai`` chain, the provider exception's own message — for surfacing a
    count/diagnostic to the user. Like a blocked field, it produces no value, so its own hard
    dependents block transitively.

    ``kind`` splits the two ways a chain ends empty-handed:

    * ``"error"`` — at least one tool BROKE (provider/network failure, bad params). This is the
      only kind a pre-tools config can produce, since the ``ai`` tool either produces or raises.
    * ``"unproductive"`` — every tool simply declined (``not_applicable``) or came up empty:
      nothing is wrong, there was just nothing to make here.
    """

    field: str
    error: str
    kind: str = "error"


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

    Generation itself is delegated to the
    :class:`~omnia.plugins.smart_notes.engine.tools.pipeline.GenerationPipeline`: the service
    owns note-level policy (ordering, blocking, chaining, skip rules) and the pipeline owns
    "how is this ONE field made". Adding a tool therefore never touches this class.
    """

    def __init__(
        self, providers: ProviderHub, *, detect_tts_language: bool = True
    ) -> None:
        # When a TTS rule pins no explicit voice, ask the LLM for the spoken text's language
        # so the voice matches it (a Vietnamese word shouldn't be read by an English voice).
        self._ctx = ToolContext(
            providers=providers,
            detector=LanguageDetector(enabled=detect_tts_language),
            logger=logger,
        )
        self._pipeline = GenerationPipeline(self._ctx)

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        Runs the rule's tool chain in order and returns the first result. A field with no
        configured chain runs the single ``"ai"`` tool, i.e. the per-kind generator this method
        used to dispatch to directly: a per-rule ``provider``/``model`` selects a provider
        INSTANCE configured with that model (the model is fixed at construction, never threaded
        per call); for TTS the spoken text is the interpolated prompt (or the interpolated
        source field when no prompt is given) and ``voice`` overrides the configured voice.
        With no explicit voice, the spoken text's language is auto-detected so the provider
        picks a matching voice. Text results are rendered from Markdown to HTML for display in
        the card.

        Raises:
            ToolChainError: When every tool in the chain declined, came up empty, or broke. It
                is a :class:`~omnia.core.providers.errors.ProviderError`, and its message is
                the chain's attempt summary — for the legacy single-``ai`` chain, the
                provider's own error message.
        """
        outcome = self._pipeline.run(rule, fields)
        if outcome.produced is None:
            raise ToolChainError(outcome.attempts)
        return outcome.produced

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

        A single field whose tool chain produces nothing (a TTS field with no Auto-detect voice,
        a provider/network error, or — with a deterministic chain — every tool declining) is
        isolated: it is recorded as a :class:`FailedField` (``kind`` says which of the two it
        was) and generation continues with the remaining fields, so one misconfigured field
        never discards siblings that already succeeded. Like a blocked field, it writes no
        value, so its own hard dependents block transitively. An error in a tool that a LATER
        tool in the same chain recovers from is absorbed: the field succeeded, which is the
        truth callers count.

        Returns:
            A tuple ``(results, blocked, failed)`` where ``results`` is the ``(rule, result)``
            pairs for the fields that actually generated (in run order), ``blocked`` lists the
            fields skipped for a missing hard prerequisite, and ``failed`` lists the fields
            whose tool chain produced nothing.

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
            outcome = self._pipeline.run(rule, working)
            if outcome.produced is None:
                # The chain is exhausted: one field's failure must not abort the note. (The
                # pipeline already logged any tool that raised, with its traceback.)
                failed.append(
                    FailedField(
                        rule.target_field,
                        outcome.summary,
                        "error" if outcome.errored else "unproductive",
                    )
                )
                # Not added to results/produced/working, so its hard dependents block
                # transitively (same as a blocked field).
                continue
            result = outcome.produced
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
