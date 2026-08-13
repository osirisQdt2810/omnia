"""Strip the IPA transcription out of a comma-separated word list.

``"modest (ˈmɒdɪst), meek (miːk)"`` → ``"modest, meek"``. A segment that does not look like
``word (…)`` is copied through untouched, so a list that is only partly annotated keeps the
parts this task does not understand.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re

from pydantic import Field

from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task

# One "word (ipa)" segment of a comma-separated list.
_SEGMENT_RE = re.compile(r"^\s*(?P<word>[^()]+?)\s*\((?P<ipa>[^()]*)\)\s*$")


class StripIpaConfig(TaskConfigBase):
    """Which field to read, and where to put the stripped list."""

    order: int = Field(
        20,
        title="Run order",
        description=(
            "Runs AFTER reformat_synonyms (10), which is what turns a collapsed list into the "
            "paired one this task understands."
        ),
    )
    fields: dict[str, str] = Field(
        default_factory=lambda: {"Synonyms": "SynonymsNoIPA"},
        title="Fields to strip",
        description=(
            "``{source field: target field}``. The stripped list is written to the target "
            "field; an EMPTY target strips the source field in place."
        ),
    )


@register_task("strip_ipa")
class StripIpaTask(MaintenanceTask):
    """Removes ``(ipa)`` annotations from each segment of a comma-separated list."""

    name = "Strip IPA"
    description = "Remove /ˈaɪ.piː.eɪ/ transcriptions from a comma-separated word list."
    config_model = StripIpaConfig

    def process(self, note: NoteView) -> dict[str, str]:
        updates: dict[str, str] = {}
        for source, target in self.config.fields.items():
            stripped = self._strip(note.field(source).strip())
            if stripped is None:
                continue
            destination = target or source
            if note.field(destination) != stripped:
                updates[destination] = stripped
        return updates

    @staticmethod
    def _strip(raw: str) -> str | None:
        """Return the list with every ``(ipa)`` removed, or None if nothing was annotated."""
        segments = [part.strip() for part in raw.split(",") if part.strip()]
        words: list[str] = []
        stripped_any = False
        for segment in segments:
            match = _SEGMENT_RE.match(segment)
            word = (match.group("word") or "").strip() if match else ""
            if not word:
                words.append(segment)  # not "word (ipa)" — keep it exactly as written
                continue
            words.append(word)
            stripped_any = stripped_any or word != segment
        if not (words and stripped_any):
            return None
        cleaned = ", ".join(words)
        return cleaned if cleaned != raw else None
