"""Anki glue for smart-notes: batch, editor button, field menu, and review-time generation.

Re-exports the integration public surface so the plugin imports from
``smart_notes.integration`` rather than its submodules. These modules MAY import ``aqt``/
``anki`` (directly or lazily) — they are the impure glue around the pure ``engine``.
"""

from __future__ import annotations

from omnia.plugins.smart_notes.integration.batch import (
    BatchGenerator,
    BatchSummary,
    materialize,
)
from omnia.plugins.smart_notes.integration.editor import (
    add_generate_button,
    set_button_enabled,
)
from omnia.plugins.smart_notes.integration.field_menu import (
    build_field_menu,
    single_field_config,
)
from omnia.plugins.smart_notes.integration.gateway import IntegrationGateway
from omnia.plugins.smart_notes.integration.integrations import (
    AUTOGEN_TAG,
    INTEGRATIONS,
    Integration,
    integration_for_tags,
)
from omnia.plugins.smart_notes.integration.review import ReviewTimeEvaluator
from omnia.plugins.smart_notes.integration.store import SmartNotesStore

__all__ = [
    "AUTOGEN_TAG",
    "INTEGRATIONS",
    "BatchGenerator",
    "BatchSummary",
    "Integration",
    "IntegrationGateway",
    "ReviewTimeEvaluator",
    "SmartNotesStore",
    "add_generate_button",
    "build_field_menu",
    "integration_for_tags",
    "materialize",
    "set_button_enabled",
    "single_field_config",
]
