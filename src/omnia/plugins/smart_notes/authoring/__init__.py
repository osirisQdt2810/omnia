"""Prompt authoring for smart-notes: auto-smart inference + rough-prompt refinement.

Re-exports the authoring public surface so callers import from ``smart_notes.authoring``
rather than its submodules. No Anki imports — pure logic, unit-tested headless.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.authoring.author import (
    PromptAuthor,
    apply_auto_smart,
    build_auto_smart_prompt,
    build_classify_deps_batch_message,
    build_classify_deps_message,
    build_edge_change_message,
    build_improve_in_popover_message,
    candidate_fields,
    parse_auto_smart_response,
    parse_classified_deps,
    parse_classified_deps_batch,
)
from omnia.plugins.smart_notes.authoring.models import (
    AutoSmartDep,
    AutoSmartField,
    EdgeChange,
    EdgeKinding,
    PromptRewrite,
)
from omnia.plugins.smart_notes.authoring.persona import (
    DEPENDENCY_CLASSIFIER_SYSTEM,
    FLASHCARD_EXPERT_SYSTEM,
)

__all__ = [
    "DEPENDENCY_CLASSIFIER_SYSTEM",
    "FLASHCARD_EXPERT_SYSTEM",
    "AutoSmartDep",
    "AutoSmartField",
    "EdgeChange",
    "EdgeKinding",
    "PromptAuthor",
    "PromptRewrite",
    "apply_auto_smart",
    "build_auto_smart_prompt",
    "build_classify_deps_batch_message",
    "build_classify_deps_message",
    "build_edge_change_message",
    "build_improve_in_popover_message",
    "candidate_fields",
    "parse_auto_smart_response",
    "parse_classified_deps",
    "parse_classified_deps_batch",
]
