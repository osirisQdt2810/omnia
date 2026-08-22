"""The pure smart-notes generation engine: rules, ordering, and provider-backed generators.

Re-exports the engine's public surface so callers import from ``smart_notes.engine`` rather
than its submodules. No Anki imports — the whole engine unit-tests headless.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.engine.batching import (
    SOLO_PLANNER,
    FieldBatchRunner,
    FieldWork,
    SoloPlanner,
    WavePlanner,
    WaveTask,
    run_wave,
)
from omnia.plugins.smart_notes.engine.consistency import (
    ConsistencyResult,
    NodeEdgeSet,
    validate_prompt_syntax,
)
from omnia.plugins.smart_notes.engine.generators import (
    GenerationResult,
    Generator,
    ImageGenerator,
    LanguageDetector,
    ResolvedVoice,
    TextGenerator,
    TTSGenerator,
)
from omnia.plugins.smart_notes.engine.graph import (
    FieldGraph,
    FieldNode,
    GraphEdge,
)
from omnia.plugins.smart_notes.engine.interpolation import (
    extract_field_refs,
    interpolate,
    validate_brace_syntax,
)
from omnia.plugins.smart_notes.engine.markdown import convert_markdown_to_html
from omnia.plugins.smart_notes.engine.note_run import NoteRun
from omnia.plugins.smart_notes.engine.ordering import (
    SmartNotesCycleError,
    order_rule_levels,
    order_rules,
)
from omnia.plugins.smart_notes.engine.rules import (
    applies_to_deck,
    compile_field_rule,
    compile_note_type_rules,
    dedupe_preserving_order,
    reconcile_field_deps,
    should_skip_rule,
)
from omnia.plugins.smart_notes.engine.service import (
    BlockedField,
    FailedField,
    GenerationService,
)
from omnia.plugins.smart_notes.engine.tools import (
    Empty,
    GenerationPipeline,
    NotApplicable,
    PipelineResult,
    Produced,
    Tool,
    ToolAttempt,
    ToolChainError,
    ToolContext,
    ToolError,
    ToolRequest,
    register_tool,
    resolve_tool,
    tools_catalog,
)

__all__ = [
    "SOLO_PLANNER",
    "BlockedField",
    "ConsistencyResult",
    "Empty",
    "FailedField",
    "FieldBatchRunner",
    "FieldGraph",
    "FieldNode",
    "FieldWork",
    "GenerationPipeline",
    "GenerationResult",
    "GenerationService",
    "Generator",
    "GraphEdge",
    "ImageGenerator",
    "LanguageDetector",
    "NodeEdgeSet",
    "NotApplicable",
    "NoteRun",
    "PipelineResult",
    "Produced",
    "ResolvedVoice",
    "SmartNotesCycleError",
    "SoloPlanner",
    "TTSGenerator",
    "TextGenerator",
    "Tool",
    "ToolAttempt",
    "ToolChainError",
    "ToolContext",
    "ToolError",
    "ToolRequest",
    "WavePlanner",
    "WaveTask",
    "applies_to_deck",
    "compile_field_rule",
    "compile_note_type_rules",
    "convert_markdown_to_html",
    "dedupe_preserving_order",
    "extract_field_refs",
    "interpolate",
    "order_rule_levels",
    "order_rules",
    "reconcile_field_deps",
    "register_tool",
    "resolve_tool",
    "run_wave",
    "should_skip_rule",
    "tools_catalog",
    "validate_brace_syntax",
    "validate_prompt_syntax",
]
