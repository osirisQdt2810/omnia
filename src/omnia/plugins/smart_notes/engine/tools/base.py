"""The Tool contract: what a generation tool is handed, and what it may hand back.

A *tool* is one way to fill one generated field. A field carries an ORDERED chain of them and
the :class:`~omnia.plugins.smart_notes.engine.tools.pipeline.GenerationPipeline` runs the chain
until one produces a result — so a deterministic tool can decline (no LLM spend) and let the
provider-backed ``"ai"`` tool take over. This module holds only the contract; the concrete
tools live beside it and self-register through
:func:`~omnia.plugins.smart_notes.engine.tools.registry.register_tool`.

The outcome taxonomy is the heart of the fallback semantics:

* :class:`Produced` — the field's content; the chain stops here.
* :class:`NotApplicable` — a precondition was unmet, so the tool never ran its transform. Falls
  through silently; this is what the whole feature exists for.
* :class:`Empty` — the tool ran and got nothing meaningful. Falls through too, but is
  distinguishable in the trace from "couldn't even try".
* *breakage is NOT a return value* — a tool RAISES (:class:`ToolError`, or anything else). The
  pipeline records the attempt as an error and still falls through, which keeps the
  raise-on-failure contract the generators already have
  (:meth:`~omnia.plugins.smart_notes.engine.generators.Generator.generate`).

Pure logic — no ``aqt``/``anki`` imports, so tools unit-test headless.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from pydantic import BaseModel

    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.generators import GenerationResult
    from omnia.plugins.smart_notes.engine.language import LanguageDetector


class ToolError(ProviderError):
    """Raised by a tool that BROKE (bad params, provider failure, unusable input).

    A :class:`~omnia.core.providers.errors.ProviderError` subclass, mirroring
    :class:`~omnia.plugins.smart_notes.engine.ordering.SmartNotesCycleError`, so every caller
    that already handles provider failures handles a tool failure too. Raising is the ONLY way
    a tool reports breakage — the three outcome types all mean "no result, and that's fine".
    """


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may touch. Built once per :class:`GenerationService`.

    Deliberately narrow (ISP): a tool gets the provider hub, the best-effort language detector
    the TTS path needs, and a logger — no collection, no config store, no Anki.
    """

    providers: ProviderHub
    detector: LanguageDetector
    logger: logging.Logger


@dataclass(frozen=True)
class ToolRequest:
    """One tool invocation: the compiled rule, the note's fields, and this tool's params.

    ``fields`` is the working map :meth:`GenerationService.generate_note` maintains, so a tool
    reads freshly chained values exactly as the generators do. ``params`` are the field's
    per-tool params AFTER :meth:`Tool.parse_params` validated them (defaults filled in).
    """

    rule: SmartNotesFieldRule
    fields: Mapping[str, str]
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Produced:
    """The tool generated the field's content — the chain stops here."""

    result: GenerationResult


@dataclass(frozen=True)
class NotApplicable:
    """A precondition was unmet, so the tool declined to run (never a failure).

    ``reason`` is the human-readable precondition ("word 'run' not found in Sentence"); it is
    recorded in the pipeline trace and shown in diagnostics.
    """

    reason: str = ""


@dataclass(frozen=True)
class Empty:
    """The tool ran its transform and got nothing meaningful back (dictionary miss, no match)."""

    reason: str = ""


ToolOutcome = Produced | NotApplicable | Empty


class Tool(ABC):
    """One way to fill one generated field.

    Subclasses are **stateless** and must be constructible with no arguments — the registry
    instantiates them on resolve and hands everything they need through :meth:`run`'s
    ``request``/``ctx`` (DIP), so the same instance is safe on any worker thread.

    Class attributes:
        name: The stable config key the field's chain stores (``"ai"``, ``"cloze"``, …).
        label: Short human name for the tools picker.
        description: One line explaining what the tool does, shown in the picker.
        kinds: The generation kinds it can serve (subset of ``{"text", "image", "tts"}``). The
            pipeline skips a tool whose ``kinds`` do not cover the rule's kind.
        deterministic: True when the tool never calls a paid/LLM endpoint.
        params_model: Pydantic model validating the field's per-tool params (None = no params).
    """

    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    kinds: ClassVar[frozenset[str]]
    deterministic: ClassVar[bool]
    params_model: ClassVar[Optional[type[BaseModel]]] = None

    @abstractmethod
    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Try to generate ``request``'s field.

        Args:
            request: The rule, the note's working field map, and the validated params.
            ctx: The providers/detector/logger the tool may use.

        Returns:
            :class:`Produced` with the content, or :class:`NotApplicable` /:class:`Empty` to
            let the next tool in the chain try.

        Raises:
            ToolError: When the tool BROKE. The pipeline records it and falls through, so a
                later tool can still fill the field.
        """

    @classmethod
    def parse_params(cls, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate ``params`` against :attr:`params_model`, filling in its defaults.

        Called by the pipeline immediately before :meth:`run`, INSIDE the attempt's try-block:
        a params model that rejects the stored dict turns that tool into an error attempt and
        the chain continues, rather than failing the whole field.

        Args:
            params: The field's raw per-tool params, as stored in config.

        Returns:
            The validated params (the raw mapping as a dict when the tool declares no model).

        Raises:
            pydantic.ValidationError: If ``params`` does not satisfy :attr:`params_model`.
        """
        if cls.params_model is None:
            return dict(params)
        validated: dict[str, Any] = cls.params_model(**params).dict()
        return validated

    @classmethod
    def referenced_fields(cls, params: Mapping[str, Any]) -> list[str]:
        """Return the note fields ``params`` names (e.g. a ``sentence_field`` param).

        These join the rule's derived prerequisites in
        :func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`, so a tool that
        reads another field orders and blocks through the SAME single source of truth the
        prompt ``{{refs}}`` use — the dependency graph and the topological sort inherit tool
        edges with no extra plumbing.

        Implementations MUST NOT raise: this runs on the ordering/graph path with params that
        have not been through :meth:`parse_params`, so read them defensively.

        Args:
            params: The field's raw per-tool params.

        Returns:
            Field names in the order they should be considered (empty by default).
        """
        return []

    @classmethod
    def availability(cls, ctx: ToolContext) -> str | None:
        """Return why the tool cannot be offered right now, or None when it is available.

        The string is shown next to a grayed-out entry in the tools picker (e.g. "needs a WAV
        TTS provider (piper/viet-tts)"). Availability is advisory: an unavailable tool still
        runs if configured, and declines with :class:`NotApplicable`.
        """
        return None
