"""Generation tools: the ordered, per-field strategies the pipeline runs.

Re-exports the tool seam (contract, registry, pipeline) so callers import from
``smart_notes.engine.tools`` rather than its submodules, and imports every builtin tool module
so its ``@register_tool`` runs at package import — the registry is empty until they do (the
same trick as ``core/providers/tts/__init__.py`` and ``plugins/__init__.py``).
"""

from __future__ import annotations

# Import every builtin tool module for its registration side effect (must come first: the
# registry is populated by these imports).
from omnia.plugins.smart_notes.engine.tools import ai, cloze, cloze_audio
from omnia.plugins.smart_notes.engine.tools.ai import AiTool
from omnia.plugins.smart_notes.engine.tools.base import (
    INPUT_KIND_EXTENSIONS,
    INPUT_KINDS,
    TEXT_INPUT,
    Empty,
    NotApplicable,
    Produced,
    Tool,
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolRequest,
    resolve_media_dir,
)
from omnia.plugins.smart_notes.engine.tools.cloze import (
    ClozeParams,
    ClozeRewriter,
    ClozeTool,
)
from omnia.plugins.smart_notes.engine.tools.cloze_audio import (
    ClozeAudioParams,
    ClozeAudioTool,
    ClozeMaskPlanner,
    MaskedAudioBuilder,
    MaskedSpeech,
    SidecarCodec,
    SpeechCodec,
    WavCodec,
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
    tool_required_params,
    tools_catalog,
    unregister_tool,
)
from omnia.plugins.smart_notes.engine.tools.user_tools import (
    GENERATION_KINDS,
    SAMPLE_FIELD,
    USER_TOOL_PREFIX,
    ImportGuard,
    ReviewGate,
    ToolInput,
    ToolTestResult,
    UserToolError,
    UserToolLoad,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    UserToolTester,
    declared_inputs,
    is_user_tool,
    risky_operations,
    slugify,
    user_tool_name,
    validate_slug,
)

__all__ = [
    "GENERATION_KINDS",
    "INPUT_KINDS",
    "INPUT_KIND_EXTENSIONS",
    "SAMPLE_FIELD",
    "TEXT_INPUT",
    "TOOL_REGISTRY",
    "USER_TOOL_PREFIX",
    "AiTool",
    "AttemptStatus",
    "ClozeAudioParams",
    "ClozeAudioTool",
    "ClozeMaskPlanner",
    "ClozeParams",
    "ClozeRewriter",
    "ClozeTool",
    "Empty",
    "GenerationPipeline",
    "ImportGuard",
    "MaskedAudioBuilder",
    "MaskedSpeech",
    "NotApplicable",
    "PipelineResult",
    "Produced",
    "ReviewGate",
    "SidecarCodec",
    "SpeechCodec",
    "Tool",
    "ToolAttempt",
    "ToolChainError",
    "ToolContext",
    "ToolError",
    "ToolInput",
    "ToolOutcome",
    "ToolRequest",
    "ToolTestResult",
    "UserToolError",
    "UserToolLoad",
    "UserToolLoader",
    "UserToolSource",
    "UserToolStore",
    "UserToolTester",
    "WavCodec",
    "ai",
    "cloze",
    "cloze_audio",
    "declared_inputs",
    "get_tool",
    "is_user_tool",
    "register_tool",
    "registered_tools",
    "resolve_media_dir",
    "resolve_tool",
    "risky_operations",
    "slugify",
    "summarize_attempts",
    "tool_referenced_fields",
    "tool_required_params",
    "tools_catalog",
    "unregister_tool",
    "user_tool_name",
    "validate_slug",
]
