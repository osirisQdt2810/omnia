"""Generation tools: the ordered, per-field strategies the pipeline runs.

Re-exports the tool seam (contract, registry, pipeline) so callers import from
``smart_notes.engine.tools`` rather than its submodules, and imports every builtin tool module
so its ``@register_tool`` runs at package import — the registry is empty until they do (the
same trick as ``core/providers/tts/__init__.py`` and ``plugins/__init__.py``).
"""

from __future__ import annotations

# Import every builtin tool module for its registration side effect (must come first: the
# registry is populated by these imports).
from omnia.plugins.smart_notes.engine.tools import ai, cloze
from omnia.plugins.smart_notes.engine.tools.ai import AiTool
from omnia.plugins.smart_notes.engine.tools.base import (
    Empty,
    NotApplicable,
    Produced,
    Tool,
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolRequest,
)
from omnia.plugins.smart_notes.engine.tools.cloze import (
    ClozeParams,
    ClozeRewriter,
    ClozeTool,
)
from omnia.plugins.smart_notes.engine.tools.pipeline import (
    AttemptStatus,
    GenerationPipeline,
    PipelineResult,
    ToolAttempt,
    ToolChainError,
    summarize_attempts,
)
from omnia.plugins.smart_notes.engine.tools.registry import (
    TOOL_REGISTRY,
    get_tool,
    register_tool,
    registered_tools,
    resolve_tool,
    tool_referenced_fields,
    tools_catalog,
)

__all__ = [
    "TOOL_REGISTRY",
    "AiTool",
    "AttemptStatus",
    "ClozeParams",
    "ClozeRewriter",
    "ClozeTool",
    "Empty",
    "GenerationPipeline",
    "NotApplicable",
    "PipelineResult",
    "Produced",
    "Tool",
    "ToolAttempt",
    "ToolChainError",
    "ToolContext",
    "ToolError",
    "ToolOutcome",
    "ToolRequest",
    "ai",
    "cloze",
    "get_tool",
    "register_tool",
    "registered_tools",
    "resolve_tool",
    "summarize_attempts",
    "tool_referenced_fields",
    "tools_catalog",
]
