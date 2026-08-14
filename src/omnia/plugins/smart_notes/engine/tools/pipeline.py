"""The per-field generation pipeline: run a rule's tool chain until one produces.

One choke point replaces the service's old one-strategy-per-kind dispatch. Tools run in
exactly the user's configured order — there is no implicit reordering — and the FIRST
:class:`~omnia.plugins.smart_notes.engine.tools.base.Produced` wins. Everything else falls
through to the next tool and is recorded in the trace:

===============  ==========================================================================
attempt status   meaning
===============  ==========================================================================
``produced``     the tool generated the content (terminal)
``not_applicable`` a precondition was unmet — the tool declined
``empty``        the tool ran and got nothing meaningful
``error``        the tool RAISED; logged with its traceback, chain continues
``unknown_tool`` no tool of that name is installed (e.g. a user tool from another device)
``wrong_kind``   the tool cannot serve this field's kind (config drift)
===============  ==========================================================================

``unknown_tool`` and ``wrong_kind`` degrade GRACEFULLY on purpose: a stale name in a synced
config must not fail the field when a later tool can still fill it.

The guard covers the WHOLE attempt — resolving the tool and reading its class attributes as
much as running it — so a tool whose constructor raises, or whose class omits ``kinds``, is
recorded as an error attempt like any other breakage instead of escaping ``run()`` and
discarding the note's sibling fields that already generated.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, Optional

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.engine.tools.base import (
    Empty,
    NotApplicable,
    Produced,
    ToolRequest,
)
from omnia.plugins.smart_notes.engine.tools.registry import resolve_tool

if TYPE_CHECKING:
    from collections.abc import Mapping

    from omnia.plugins.smart_notes.config import CompiledToolSpec, SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.generators import GenerationResult
    from omnia.plugins.smart_notes.engine.tools.base import ToolContext

AttemptStatus = Literal[
    "produced", "not_applicable", "empty", "error", "unknown_tool", "wrong_kind"
]

# What a status MEANS, for the times an attempt carries no detail of its own. These strings
# reach the USER: an exhausted chain becomes a ToolChainError, which SmartNotesContext.friendly()
# prints verbatim in the preview, the prompt palette and the account dialog. So the raw status
# token — a config-level enum value — must never be shown on its own. (The batch summary renders
# only a COUNT of failed fields; it is not this string's consumer.)
_STATUS_SENTENCES: dict[str, str] = {
    "not_applicable": "the tool did not apply to this field",
    "empty": "the tool ran but produced nothing",
    "error": "the tool failed",
    "unknown_tool": "no such tool is installed",
    "wrong_kind": "the tool cannot generate this field's type",
}


@dataclass(frozen=True)
class ToolAttempt:
    """One tool's turn in the chain: which tool, how it ended, and why."""

    tool: str
    status: AttemptStatus
    detail: str = ""
    # The exception that ended this attempt, kept so an exhausted chain can re-raise WITH its
    # cause. Without it a legacy one-`ai` chain silently upgraded a non-ProviderError (say a
    # KeyError from a bad template) into a ProviderError, which the UI prints verbatim — the
    # user went from "Preview failed — see logs." to a raw "'Word'". Excluded from equality so
    # attempt traces stay comparable in tests.
    error: Optional[Exception] = dataclass_field(
        default=None, compare=False, repr=False
    )

    @property
    def text(self) -> str:
        """The attempt's reason in words: its own ``detail``, else what its status means."""
        return self.detail or _STATUS_SENTENCES.get(self.status, self.status)


def summarize_attempts(attempts: tuple[ToolAttempt, ...]) -> str:
    """Render a chain trace as one line: ``"cloze: word not found; ai: HTTP 401"``.

    A SINGLE-attempt chain renders as just that attempt's reason, with no ``"<tool>: "``
    prefix. That is deliberate: every field configured before tool chains existed compiles to
    the lone ``"ai"`` tool, so its failure message stays byte-identical to the message the
    provider itself raised — the prefix only earns its place once there is a chain to
    disambiguate. An attempt with no detail of its own falls back to a sentence rather than to
    its status token (see :attr:`ToolAttempt.text`).
    """
    if not attempts:
        return "no tools configured for this field"
    if len(attempts) == 1:
        return attempts[0].text
    return "; ".join(f"{attempt.tool}: {attempt.text}" for attempt in attempts)


@dataclass(frozen=True)
class PipelineResult:
    """The outcome of running one field's tool chain: what it made, and the full trace."""

    produced: Optional[GenerationResult]  # None = the chain was exhausted
    attempts: tuple[ToolAttempt, ...]  # every attempt, in execution order

    @property
    def errored(self) -> bool:
        """Whether the chain was exhausted AND at least one tool broke.

        A mid-chain error followed by a later success is ABSORBED (it stays in the trace but
        the field succeeded), so this distinguishes "the providers are down" from "every tool
        simply declined".
        """
        return self.produced is None and any(
            attempt.status == "error" for attempt in self.attempts
        )

    @property
    def summary(self) -> str:
        """One-line trace of the attempts (see :func:`summarize_attempts`)."""
        return summarize_attempts(self.attempts)


class ToolChainError(ProviderError):
    """Raised when a field's whole tool chain ran without producing anything.

    Carries the attempt trace so a caller can explain WHY (``"cloze: word not found; ai: HTTP
    401"``). It subclasses :class:`~omnia.core.providers.errors.ProviderError`, so every
    caller that already handles a provider failure handles an exhausted chain too.
    """

    def __init__(self, attempts: tuple[ToolAttempt, ...]) -> None:
        super().__init__(summarize_attempts(attempts))
        self.attempts = attempts

    @property
    def cause(self) -> Optional[Exception]:
        """The last attempt's own exception, if one ended the chain.

        Lets a caller re-raise ``from`` it (and read a provider's ``status_code``) instead of
        seeing only this class's flattened message.
        """
        for attempt in reversed(self.attempts):
            if attempt.error is not None:
                return attempt.error
        return None


class GenerationPipeline:
    """Runs a rule's ordered tool chain against the shared :class:`ToolContext`."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def run(
        self, rule: SmartNotesFieldRule, fields: Mapping[str, str]
    ) -> PipelineResult:
        """Run ``rule``'s tools in order and return the first result, plus the full trace.

        Args:
            rule: The compiled rule, whose ``tools`` chain is run in its configured order.
            fields: The note's working field map (freshly chained values included).

        Returns:
            A :class:`PipelineResult` whose ``produced`` is the winning tool's result, or None
            when every tool declined, came up empty, or broke.
        """
        attempts: list[ToolAttempt] = []
        for spec in rule.tools:
            try:
                attempt, result = self._attempt(spec, rule, fields)
            except Exception as exc:  # a broken tool must not fail the whole chain
                self._ctx.logger.exception(
                    "smart_notes: tool %r failed for field %r",
                    spec.name,
                    rule.target_field,
                )
                attempt, result = (
                    ToolAttempt(spec.name, "error", str(exc), error=exc),
                    None,
                )
            attempts.append(attempt)
            if attempt.status == "produced":
                return PipelineResult(result, tuple(attempts))
        return PipelineResult(None, tuple(attempts))

    def _attempt(
        self,
        spec: CompiledToolSpec,
        rule: SmartNotesFieldRule,
        fields: Mapping[str, str],
    ) -> tuple[ToolAttempt, Optional[GenerationResult]]:
        """Resolve ONE tool and give it its turn, returning its attempt and any result.

        Every failure mode lives inside the caller's guard, deliberately: resolution builds the
        tool (a constructor may raise) and the kind gate reads a ClassVar (a malformed tool
        class may not have one), so doing either outside :meth:`run`'s ``try`` would let one bad
        tool abort the whole note.

        Args:
            spec: The chain entry to run (tool name + its stored params).
            rule: The compiled rule being generated.
            fields: The note's working field map.

        Returns:
            The attempt to record, and the produced result (None unless the status is
            ``"produced"``).
        """
        tool = resolve_tool(spec.name)
        if tool is None:
            return (
                ToolAttempt(spec.name, "unknown_tool", f"no tool named {spec.name!r}"),
                None,
            )
        if rule.kind not in tool.kinds:
            return (
                ToolAttempt(
                    spec.name,
                    "wrong_kind",
                    f"{spec.name!r} cannot generate {rule.kind!r} fields",
                ),
                None,
            )
        request = ToolRequest(
            rule=rule, fields=fields, params=tool.parse_params(spec.params)
        )
        outcome = tool.run(request, self._ctx)
        match outcome:
            case Produced(result=result):
                # Stamp WHICH tool made it: the result is the only thing that travels back out
                # of the chain, so it carries the provenance the batch summary counts.
                return ToolAttempt(spec.name, "produced"), replace(
                    result, tool=spec.name
                )
            case NotApplicable(reason=reason):
                return ToolAttempt(spec.name, "not_applicable", reason), None
            case Empty(reason=reason):
                return ToolAttempt(spec.name, "empty", reason), None
            case _:
                # A tool that returns something outside the outcome union is broken; treat it
                # exactly like a raise so the chain still falls through.
                return (
                    ToolAttempt(
                        spec.name, "error", f"returned {outcome!r}, not a ToolOutcome"
                    ),
                    None,
                )
