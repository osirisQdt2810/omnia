"""The provider-backed generation service that runs smart-notes field rules.

No Anki imports. :meth:`GenerationService.generate` runs one rule by handing it to the
:class:`~omnia.plugins.smart_notes.engine.tools.pipeline.GenerationPipeline`, which tries the
rule's ordered tool chain until one produces (a field with no configured chain compiles to the
single ``"ai"`` tool — the provider-backed path — so behaviour is unchanged);
:meth:`GenerationService.generate_note` compiles a
:class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` into rules
(:func:`~omnia.plugins.smart_notes.engine.rules.compile_note_type_rules`) and walks them one
dependency LEVEL at a time (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rule_levels`),
chaining each text result into the field map so a downstream rule sees the freshly generated
value. The note-level state and policy live in
:class:`~omnia.plugins.smart_notes.engine.note_run.NoteRun`; this class owns only "build the
rules, make the work units, hand them to a dispatch".

That last split is the whole point of the two seams here. The engine says WHICH fields may run
together; the injected :class:`~omnia.core.concurrency.dispatch.Dispatch` says how
they are actually run — and defaults to sequential, so the engine stays deterministic and no
existing caller changes behaviour. A batch runner that wants many notes in flight builds the
:class:`NoteRun` objects itself (:meth:`make_run`) and pools their levels together
(:meth:`works_for`), reusing this exact per-note semantics instead of mirroring it.

The injected :class:`~omnia.core.providers.ProviderHub` keeps it testable with a fake hub (DIP).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Optional

from omnia.core.concurrency.dispatch import SEQUENTIAL_DISPATCH, Dispatch
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine.batching import (
    SOLO_PLANNER,
    FieldBatchRunner,
    FieldWork,
    WavePlanner,
)
from omnia.plugins.smart_notes.engine.generators import LanguageDetector
from omnia.plugins.smart_notes.engine.note_run import (
    BlockedField,
    FailedField,
    NoteRun,
)
from omnia.plugins.smart_notes.engine.rules import compile_note_type_rules
from omnia.plugins.smart_notes.engine.tools import (
    GenerationPipeline,
    ToolChainError,
    ToolContext,
    resolve_media_dir,
)

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )
    from omnia.plugins.smart_notes.engine.generators import GenerationResult

logger = get_logger("smart_notes")

# Re-exported: BlockedField/FailedField moved next to the NoteRun that produces them, but every
# caller (and ``engine/__init__``) imports them from here.
__all__ = ["BlockedField", "FailedField", "GenerationService"]


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
            # Passed as the FUNCTION, not its result: this constructor runs on the Qt main
            # thread while a tool runs on a worker, and resolving the collection here would
            # both touch Anki at build time and break every headless test of this service.
            media_dir=resolve_media_dir,
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
            chain_error = ToolChainError(outcome.attempts)
            # `from` the tool's own exception: a caller that inspects the cause still sees
            # a provider's status_code, and the traceback keeps the real origin.
            raise chain_error from chain_error.cause
        return outcome.produced

    def make_run(
        self,
        config: SmartNotesNoteTypeConfig,
        fields: dict[str, str],
        *,
        allow_empty_fields: bool = False,
        force_overwrite: bool = False,
        materialize: Optional[
            Callable[[SmartNotesFieldRule, GenerationResult], str]
        ] = None,
        note_id: int = 0,
    ) -> NoteRun:
        """Compile ``config`` into rules and return the :class:`NoteRun` that walks them.

        The seam a caller uses when it wants to interleave several notes: it holds many runs,
        advances each one level at a time, and pools the levels together. :meth:`generate_note`
        is the same thing for exactly one note.

        Raises:
            SmartNotesCycleError: If the fields reference each other in a cycle.
        """
        return NoteRun(
            compile_note_type_rules(config),
            fields,
            note_id=note_id,
            allow_empty_fields=allow_empty_fields,
            force_overwrite=force_overwrite,
            materialize=materialize,
        )

    def works_for(self, run: NoteRun) -> list[FieldWork]:
        """Advance ``run`` to its next level and return that level's work, one item per field.

        A :class:`~omnia.plugins.smart_notes.engine.batching.FieldWork` carries the rule, the
        level's FROZEN field snapshot, and a ``solo`` thunk over that one field's tool chain.
        Nothing a unit touches is written by a sibling unit, which is what makes it safe to hand
        the whole list to a pool. The caller must pass the outcomes back to ``run.commit`` in
        the same order.

        Descriptors rather than bare thunks because a caller that batches several notes into one
        request has to SEE the rule and its inputs to group them — and must still be able to run
        any one of them alone when the grouped call cannot answer for it.
        """
        level = run.next_dispatch()
        snapshot = run.snapshot
        return [
            FieldWork(
                rule=rule,
                fields=snapshot,
                note_id=run.note_id,
                solo=partial(self._pipeline.run, rule, snapshot, note_id=run.note_id),
            )
            for rule in level
        ]

    def batch_planner(self, *, notes_per_call: int) -> WavePlanner:
        """Return the planner a batch run's waves go through.

        ``notes_per_call <= 1`` — the shipped default — returns the SOLO planner, so "batching
        off" is the pre-batching code path rather than the batching code path configured to a
        width of one: no envelope, no ids, no parsing, nothing to get wrong.
        """
        if notes_per_call <= 1:
            return SOLO_PLANNER
        return FieldBatchRunner(self._ctx.providers, notes_per_call=notes_per_call)

    def generate_note(
        self,
        config: SmartNotesNoteTypeConfig,
        fields: dict[str, str],
        *,
        allow_empty_fields: bool = False,
        force_overwrite: bool = False,
        materialize: Optional[
            Callable[[SmartNotesFieldRule, GenerationResult], str]
        ] = None,
        note_id: int = 0,
        dispatch: Dispatch = SEQUENTIAL_DISPATCH,
    ) -> tuple[
        list[tuple[SmartNotesFieldRule, GenerationResult]],
        list[BlockedField],
        list[FailedField],
    ]:
        """Generate a note type's enabled fields, in dependency order, with chaining.

        The note type's generatable fields are compiled into rules
        (:func:`~omnia.plugins.smart_notes.engine.rules.compile_note_type_rules`) and grouped
        into dependency LEVELS
        (:func:`~omnia.plugins.smart_notes.engine.ordering.order_rule_levels`) so a field that
        references another generated field runs after it, and each text result is written back
        into a working copy of ``fields`` so the dependent field interpolates the freshly
        generated value. The base field is never generated. Whatever ``dispatch`` does with a
        level, the returned lists come back in
        :func:`~omnia.plugins.smart_notes.engine.ordering.order_rules`' order — the same order
        the one-rule-at-a-time engine produced.

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
            materialize: Turns a media result into the string the note will hold; called on
                THIS thread, never on a dispatch worker.
            note_id: The note being generated, carried into every log line so a run with
                several notes in flight stays attributable.
            dispatch: How one level's fields are run. The default runs them one at a time in
                this thread, which is what every caller did before concurrency existed.

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
            pairs for the fields that actually generated, ``blocked`` lists the fields skipped
            for a missing hard prerequisite, and ``failed`` lists the fields whose tool chain
            produced nothing — all three in ``order_rules`` order.

        Raises:
            SmartNotesCycleError: If the fields reference each other in a cycle. (A single field's
                provider/network failure is NOT raised — it is recorded in ``failed``.)
        """
        run = self.make_run(
            config,
            fields,
            allow_empty_fields=allow_empty_fields,
            force_overwrite=force_overwrite,
            materialize=materialize,
            note_id=note_id,
        )
        while not run.done:
            works = self.works_for(run)
            run.commit(dispatch.run([work.solo for work in works]) if works else [])
        return run.finish()
