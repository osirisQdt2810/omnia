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

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from dataclasses import dataclass
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

    from omnia.plugins.smart_notes.config import SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.generators import GenerationResult
    from omnia.plugins.smart_notes.engine.tools.base import ToolContext

AttemptStatus = Literal[
    "produced", "not_applicable", "empty", "error", "unknown_tool", "wrong_kind"
]


@dataclass(frozen=True)
class ToolAttempt:
    """One tool's turn in the chain: which tool, how it ended, and why."""

    tool: str
    status: AttemptStatus
    detail: str = ""


def summarize_attempts(attempts: tuple[ToolAttempt, ...]) -> str:
    """Render a chain trace as one line: ``"cloze: word not found; ai: HTTP 401"``.

    A SINGLE-attempt chain renders as just that attempt's detail, with no ``"<tool>: "``
    prefix. That is deliberate: every field configured before tool chains existed compiles to
    the lone ``"ai"`` tool, so its failure message stays byte-identical to the message the
    provider itself raised — the prefix only earns its place once there is a chain to
    disambiguate.
    """
    if not attempts:
        return "no tools configured for this field"
    if len(attempts) == 1:
        return attempts[0].detail or attempts[0].status
    return "; ".join(
        f"{attempt.tool}: {attempt.detail or attempt.status}" for attempt in attempts
    )


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
            tool = resolve_tool(spec.name)
            if tool is None:
                attempts.append(
                    ToolAttempt(
                        spec.name, "unknown_tool", f"no tool named {spec.name!r}"
                    )
                )
                continue
            if rule.kind not in tool.kinds:
                attempts.append(
                    ToolAttempt(
                        spec.name,
                        "wrong_kind",
                        f"{spec.name!r} cannot generate {rule.kind!r} fields",
                    )
                )
                continue
            try:
                request = ToolRequest(
                    rule=rule, fields=fields, params=tool.parse_params(spec.params)
                )
                outcome = tool.run(request, self._ctx)
            except Exception as exc:  # a broken tool must not fail the whole chain
                self._ctx.logger.exception(
                    "smart_notes: tool %r failed for field %r",
                    spec.name,
                    rule.target_field,
                )
                attempts.append(ToolAttempt(spec.name, "error", str(exc)))
                continue
            match outcome:
                case Produced(result=result):
                    attempts.append(ToolAttempt(spec.name, "produced"))
                    return PipelineResult(result, tuple(attempts))
                case NotApplicable(reason=reason):
                    attempts.append(ToolAttempt(spec.name, "not_applicable", reason))
                case Empty(reason=reason):
                    attempts.append(ToolAttempt(spec.name, "empty", reason))
                case _:
                    # A tool that returns something outside the outcome union is broken; treat
                    # it exactly like a raise so the chain still falls through.
                    attempts.append(
                        ToolAttempt(
                            spec.name,
                            "error",
                            f"returned {outcome!r}, not a ToolOutcome",
                        )
                    )
        return PipelineResult(None, tuple(attempts))
