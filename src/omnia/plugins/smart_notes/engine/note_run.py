"""One note's level-by-level generation state: the gates, the chaining, and the commit.

This is the whole of ``generate_note``'s note-level policy, lifted out of its ``for`` loop and
split into the two phases that loop always had implicitly:

* a READ phase — the hard-prerequisite block gate and the skip predicate — which must see a
  consistent field map and decide, for a whole dependency level at once, what runs;
* a WRITE phase — results, ``produced``, the working map, ``materialize`` — which must apply
  in rule order.

Making the split explicit is what lets a level's fields run concurrently without their gates
racing each other's writes. Both phases run on the caller's (driver) thread; only the tool
chains between them are dispatched. That placement is not incidental:

* the skip predicate is the SILENT drop path (a skipped field is recorded nowhere), so
  evaluating it inside a worker against a map another worker is mutating would resurrect
  precisely the "no output, no error" bug this feature spent a release removing;
* ``materialize`` writes media through Anki's collection, so keeping it on the driver thread
  is what makes the per-note memo dict safe without a lock — and what makes a cancelled wave
  unable to orphan a media file.

Having ONE object own this also means the batch runner can overlap many notes without a second
implementation of the same gates drifting away from this one.

Pure logic — no ``aqt``/``anki``, no threading.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Optional, Union

from omnia.plugins.smart_notes.engine.ordering import order_rule_levels, order_rules
from omnia.plugins.smart_notes.engine.rules import rule_prerequisites, should_skip_rule

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.generators import GenerationResult
    from omnia.plugins.smart_notes.engine.tools.pipeline import PipelineResult

_Materializer = Callable[["SmartNotesFieldRule", "GenerationResult"], str]


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

    ``note_id`` is the note the field belongs to. It defaults to 0 and is carried purely so a
    log line stays attributable once several notes generate at once — with notes interleaved,
    "which note was that?" is otherwise unanswerable from ``omnia.log``.
    """

    field: str
    error: str
    kind: str = "error"
    note_id: int = 0


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


class NoteRun:
    """The generation state of ONE note, walked one dependency level at a time.

    Usage is a three-beat loop the caller drives, so the caller — not the engine — decides how
    widely each level is actually run::

        while not run.done:
            level = run.next_dispatch()          # gates, on this thread
            run.commit(dispatch.run(units))      # chaining + media, on this thread
        results, blocked, failed = run.finish()

    The caller's ``fields`` map is never mutated: a working copy is taken at construction (the
    editor path hands in the live note's values and reads them again afterwards).
    """

    def __init__(
        self,
        rules: list[SmartNotesFieldRule],
        fields: dict[str, str],
        *,
        note_id: int = 0,
        allow_empty_fields: bool = False,
        force_overwrite: bool = False,
        materialize: Optional[_Materializer] = None,
    ) -> None:
        if force_overwrite:
            rules = [rule.copy(update={"overwrite": True}) for rule in rules]
        self.note_id = note_id
        self.working: dict[str, str] = dict(fields)
        self._allow_empty_fields = allow_empty_fields
        self._materialize = materialize
        self._levels = order_rule_levels(rules)
        # Each rule's position in ``order_rules``' one-at-a-time order. Levels are a DIFFERENT
        # (equally valid) topological order, so outputs assembled level-major would come back in
        # a different sequence than they always have — for the golden fixture, [Def, Pic, Audio,
        # Usage] instead of the [Def, Usage, Pic, Audio] three tests pin. Sorting on this at
        # finish() is what keeps the observable triple identical to the sequential engine's.
        self._position = {
            id(rule): index for index, rule in enumerate(order_rules(rules))
        }
        self._level_index = 0
        self._dispatched: list[SmartNotesFieldRule] = []
        self._snapshot: Mapping[str, str] = MappingProxyType({})
        # Every output list is kept as (canonical position, value) so finish() can restore
        # order_rules' order without having to search for the rule that produced an entry.
        self._results: list[
            tuple[int, tuple[SmartNotesFieldRule, GenerationResult]]
        ] = []
        self._blocked: list[tuple[int, BlockedField]] = []
        self._failed: list[tuple[int, FailedField]] = []
        # Lower-cased target names that generated a non-error result this run. Media results
        # ARE chained (as their reference) when a materializer is supplied, but a caller that
        # supplies none still leaves them out of ``working``; ``produced`` records the success
        # either way, so a hard prerequisite on a media field stays satisfied.
        self._produced: set[str] = set()

    @property
    def done(self) -> bool:
        """Whether every dependency level has been dispatched and committed."""
        return self._level_index >= len(self._levels)

    @property
    def snapshot(self) -> Mapping[str, str]:
        """The read-only field map the CURRENT level's rules must all see.

        Frozen once per level, before any of it is dispatched, so every rule in the level reads
        the same values no matter what order they finish in. Read-only so a tool that mutates
        what it was handed fails loudly here instead of silently changing a sibling's inputs.
        """
        return self._snapshot

    def next_dispatch(self) -> list[SmartNotesFieldRule]:
        """Advance to the next level and return the rules of it that must actually run.

        Applies both gates, in rule order, on the calling thread: a rule with an unmet hard
        prerequisite is recorded as a :class:`BlockedField` and dropped; a rule the skip
        predicate rejects is dropped silently (as it always has been). Freezes
        :attr:`snapshot` for whatever remains.
        """
        level = self._levels[self._level_index]
        self._level_index += 1
        dispatch: list[SmartNotesFieldRule] = []
        for rule in level:
            missing = self._missing_hard_prerequisites(rule)
            if missing:
                self._blocked.append(
                    (
                        self._position[id(rule)],
                        BlockedField(rule.target_field, missing),
                    )
                )
                continue  # writes no value → hard dependents block transitively
            if should_skip_rule(
                rule, self.working, allow_empty_fields=self._allow_empty_fields
            ):
                continue
            dispatch.append(rule)
        self._dispatched = dispatch
        self._snapshot = MappingProxyType(dict(self.working))
        return list(dispatch)

    def commit(self, outcomes: list[Union[PipelineResult, Exception]]) -> None:
        """Apply the level's outcomes, in the order :meth:`next_dispatch` returned its rules.

        ``outcomes`` must align one-to-one with that list — which is exactly why the dispatch
        seam guarantees input order. An outcome that is an ``Exception`` is a worker that broke
        outside the pipeline's own guard; it becomes that field's :class:`FailedField` rather
        than escaping to the caller, so one field can never discard a note's siblings (nor, in
        a batch, be blamed on every note in the wave).
        """
        if len(outcomes) != len(self._dispatched):
            raise ValueError(
                f"commit got {len(outcomes)} outcomes for {len(self._dispatched)} rules"
            )
        for rule, outcome in zip(self._dispatched, outcomes, strict=True):
            position = self._position[id(rule)]
            if isinstance(outcome, Exception):
                self._failed.append(
                    (
                        position,
                        FailedField(
                            rule.target_field, str(outcome), "error", self.note_id
                        ),
                    )
                )
                continue
            if outcome.produced is None:
                # The chain is exhausted: one field's failure must not abort the note. (The
                # pipeline already logged any tool that raised, with its traceback.)
                self._failed.append(
                    (
                        position,
                        FailedField(
                            rule.target_field,
                            outcome.summary,
                            "error" if outcome.errored else "unproductive",
                            self.note_id,
                        ),
                    )
                )
                # Not added to results/produced/working, so its hard dependents block
                # transitively (same as a blocked field).
                continue
            result = outcome.produced
            self._results.append((position, (rule, result)))
            self._produced.add(rule.target_field.strip().lower())
            if result.kind == "text" and result.text is not None:
                self.working[rule.target_field] = result.text
            elif result.kind in ("image", "tts") and self._materialize is not None:
                # Media belongs in the working map too — as the REFERENCE the note will hold,
                # not as bytes. Without it a field reading an audio field sees blank, and
                # should_skip_rule drops that field before its tools are ever consulted: no
                # output, no error, nothing to explain it. That is exactly what a tool
                # extracting a filename out of [sound:…] needs to read.
                self.working[rule.target_field] = self._materialize(rule, result)
        self._dispatched = []

    def finish(
        self,
    ) -> tuple[
        list[tuple[SmartNotesFieldRule, GenerationResult]],
        list[BlockedField],
        list[FailedField],
    ]:
        """Return ``(results, blocked, failed)`` in :func:`order_rules`' canonical order.

        Sorting here — rather than trusting the order things happened to be committed in — is
        what makes the triple independent of the level structure, of the worker count and of
        completion order. Callers (and three golden tests) read these lists positionally.
        """
        return (
            [value for _, value in sorted(self._results, key=lambda item: item[0])],
            [value for _, value in sorted(self._blocked, key=lambda item: item[0])],
            [value for _, value in sorted(self._failed, key=lambda item: item[0])],
        )

    def _missing_hard_prerequisites(self, rule: SmartNotesFieldRule) -> list[str]:
        """Return the rule's hard prerequisites that are unmet (case-insensitive).

        A prerequisite is satisfied when it holds a non-blank value in the working map (an input
        field or a chained text result) OR its producing rule generated successfully this run
        (covers image/tts fields, whose embed refs are not chained into the map when no
        materializer was supplied). It is "missing" only when it is genuinely blank AND was not
        produced — the case where it was itself blocked or its generation yielded an empty
        value, which propagates the block transitively.

        Safe to evaluate one level at a time: a hard prerequisite IS an edge, so its producer
        sits in an EARLIER level and has already committed. No rule can be waiting on a sibling
        of its own level.
        """
        present = {
            name.strip().lower()
            for name, value in self.working.items()
            if str(value).strip()
        }
        present |= self._produced
        return [
            prereq
            for prereq in _hard_prerequisites(rule)
            if prereq.strip().lower() not in present
        ]
