"""Find and replace a literal string across every field of a note.

The workhorse for cleaning up a shared deck: a promo line, a stray watermark or a broken
entity that was pasted into thousands of notes. The match is LITERAL (no regex, no word
boundaries) — what the user types is what gets replaced, in every field that contains it.

An empty ``find`` is a deliberate no-op, so the task can ship enabled without touching
anything until the user says what to replace.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from pydantic import Field

from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task


class ReplaceTextAllFieldsConfig(TaskConfigBase):
    """The literal text to look for, and what to put in its place."""

    find: str = Field(
        "",
        title="Find",
        description=(
            "The exact text to replace, in EVERY field. Matched literally (not a regular "
            "expression) and case-sensitively. Empty = do nothing."
        ),
    )
    replace: str = Field(
        "",
        title="Replace with",
        description="What to put in its place. Empty = delete the matched text.",
    )


@register_task("replace_text_all_fields")
class ReplaceTextAllFieldsTask(MaintenanceTask):
    """Replaces a literal string wherever it appears in a note's fields."""

    name = "Replace text in all fields"
    description = "Replace a literal string across every field of the note."
    config_model = ReplaceTextAllFieldsConfig

    def process(self, note: NoteView) -> dict[str, str]:
        needle = self.config.find
        if not needle:
            return {}
        updates: dict[str, str] = {}
        for field in note.fields:
            value = note.field(field)
            if needle not in value:
                continue
            updates[field] = value.replace(needle, self.config.replace)
        return updates
