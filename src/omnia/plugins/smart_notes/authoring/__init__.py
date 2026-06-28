"""Prompt authoring for smart-notes: auto-smart inference + rough-prompt refinement.

Re-exports the authoring public surface so callers import from ``smart_notes.authoring``
rather than its submodules. No Anki imports — pure logic, unit-tested headless.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.authoring.author import (
    PromptAuthor,
    apply_auto_smart,
    build_auto_smart_prompt,
    candidate_fields,
    parse_auto_smart_response,
)
from omnia.plugins.smart_notes.authoring.models import AutoSmartField
from omnia.plugins.smart_notes.authoring.persona import FLASHCARD_EXPERT_SYSTEM

__all__ = [
    "FLASHCARD_EXPERT_SYSTEM",
    "AutoSmartField",
    "PromptAuthor",
    "apply_auto_smart",
    "build_auto_smart_prompt",
    "candidate_fields",
    "parse_auto_smart_response",
]
