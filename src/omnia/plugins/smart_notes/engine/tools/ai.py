"""The ``ai`` tool: smart_notes' provider-backed generation, packaged as one tool.

This is the whole pre-tools engine, unchanged. It does NOT reimplement generation — it
delegates, per kind, to the very same
:class:`~omnia.plugins.smart_notes.engine.generators.Generator` strategies the service used to
dispatch to, so a field with no configured chain (which compiles to exactly this tool) still
produces byte-identical output. It is the only non-deterministic builtin: every call costs
LLM/TTS spend, which is why deterministic tools are ordered BEFORE it in a chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from omnia.plugins.smart_notes.config import DEFAULT_TOOL_NAME
from omnia.plugins.smart_notes.engine.generators import (
    ImageGenerator,
    TextGenerator,
    TTSGenerator,
)
from omnia.plugins.smart_notes.engine.tools.base import Produced, Tool, ToolOutcome
from omnia.plugins.smart_notes.engine.tools.registry import register_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from omnia.plugins.smart_notes.engine.generators import Generator
    from omnia.plugins.smart_notes.engine.tools.base import ToolContext, ToolRequest


@register_tool(DEFAULT_TOOL_NAME)
class AiTool(Tool):
    """Generates the field with the configured LLM/TTS provider (the legacy path)."""

    # One factory per generation kind — the same strategy table the service held, moved here.
    # ``kinds`` is derived from it so the two can never drift apart.
    _FACTORIES: ClassVar[dict[str, Callable[[ToolContext], Generator]]] = {
        "text": lambda ctx: TextGenerator(ctx.providers),
        "image": lambda ctx: ImageGenerator(ctx.providers),
        "tts": lambda ctx: TTSGenerator(ctx.providers, ctx.detector),
    }

    name: ClassVar[str] = DEFAULT_TOOL_NAME
    label: ClassVar[str] = "AI"
    description: ClassVar[str] = (
        "Generate the field with the configured AI provider (LLM text/image, TTS audio)."
    )
    kinds: ClassVar[frozenset[str]] = frozenset(_FACTORIES)
    deterministic: ClassVar[bool] = False

    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Delegate to the generator for the rule's kind and wrap its result.

        The generators raise on failure (bad config, provider/network error) and the pipeline
        turns that into an error attempt, so this tool never returns
        :class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`: the AI path is
        always willing to try.
        """
        generator = self._FACTORIES[request.rule.kind](ctx)
        return Produced(generator.generate(request.rule, dict(request.fields)))
