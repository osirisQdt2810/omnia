"""The pure smart-notes generation engine: rules, ordering, and provider-backed generators.

Re-exports the engine's public surface so callers import from ``smart_notes.engine`` rather
than its submodules. No Anki imports — the whole engine unit-tests headless.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.engine.generators import (
    GenerationResult,
    Generator,
    ImageGenerator,
    LanguageDetector,
    TextGenerator,
    TTSGenerator,
)
from omnia.plugins.smart_notes.engine.graph import (
    FieldGraph,
    FieldNode,
    GraphEdge,
    build_field_graph,
    layered_layout,
    validate_acyclic,
    would_create_cycle,
)
from omnia.plugins.smart_notes.engine.interpolation import (
    extract_field_refs,
    interpolate,
)
from omnia.plugins.smart_notes.engine.markdown import convert_markdown_to_html
from omnia.plugins.smart_notes.engine.ordering import (
    SmartNotesCycleError,
    order_rules,
)
from omnia.plugins.smart_notes.engine.rules import (
    applies_to_deck,
    chunk,
    compile_note_type_rules,
    dedupe_preserving_order,
    should_skip_rule,
)
from omnia.plugins.smart_notes.engine.service import BlockedField, GenerationService

__all__ = [
    "BlockedField",
    "FieldGraph",
    "FieldNode",
    "GenerationResult",
    "GenerationService",
    "Generator",
    "GraphEdge",
    "ImageGenerator",
    "LanguageDetector",
    "SmartNotesCycleError",
    "TTSGenerator",
    "TextGenerator",
    "applies_to_deck",
    "build_field_graph",
    "chunk",
    "compile_note_type_rules",
    "convert_markdown_to_html",
    "dedupe_preserving_order",
    "extract_field_refs",
    "interpolate",
    "layered_layout",
    "order_rules",
    "should_skip_rule",
    "validate_acyclic",
    "would_create_cycle",
]
