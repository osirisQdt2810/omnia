"""Refill a plain example sentence from its clozed twin when the two have drifted apart.

Two fields hold the same sentence — one plain, one with the target word clozed. Editing only
the clozed one (the usual direction: that is the field being reviewed) leaves the plain field
stale. This task copies the clozed field over when the two no longer say the same thing.

"The same thing" is measured on the WORDS, not the markup: both sides are reduced to plain
text (:func:`~omnia.core.text.strip_markup`, which also unwraps cloze deletions to their
answer) and compared by Jaccard similarity, so ``<b>`` tags, ``{{c1::…}}`` wrappers and
whitespace never count as a difference.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re

from pydantic import Field

from omnia.core.text import strip_markup
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*|\d+", re.UNICODE)


class FillFirstExampleConfig(TaskConfigBase):
    """Which fields hold the two copies of the sentence, and how different is too different."""

    source_field: str = Field(
        "Clozed First Example",
        title="Source field",
        description="The field that is kept up to date (usually the clozed sentence).",
    )
    target_field: str = Field(
        "First Example",
        title="Target field",
        description="The field refilled from the source when the two have drifted apart.",
    )
    threshold: float = Field(
        0.75,
        ge=0.0,
        le=1.0,
        title="Similarity threshold",
        description=(
            "How alike the two sentences must be (0–1, on their words) to be left alone.\n"
            "• 1.0 = refill unless they are word-for-word identical.\n"
            "• 0.0 = never refill."
        ),
    )


@register_task("fill_first_example")
class FillFirstExampleTask(MaintenanceTask):
    """Copies the source sentence into the target field when the two have drifted apart."""

    name = "Fill first example"
    description = (
        "Refill the plain example sentence from its clozed twin when they differ."
    )
    config_model = FillFirstExampleConfig

    def process(self, note: NoteView) -> dict[str, str]:
        source = note.field(self.config.source_field)
        if not source.strip():
            return {}
        target = note.field(self.config.target_field)
        if target.strip() == source.strip():
            return {}
        if self._similarity(target, source) >= self.config.threshold:
            return {}
        return {self.config.target_field: source}

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        """Return the Jaccard similarity of two fields' words, ignoring their markup."""
        left_words = _words(left)
        right_words = _words(right)
        if not (left_words or right_words):
            return 1.0
        if not (left_words and right_words):
            return 0.0
        return len(left_words & right_words) / len(left_words | right_words)


def _words(value: str) -> set[str]:
    """Return the lower-cased words of ``value`` with its markup and cloze wrappers removed."""
    return set(_WORD_RE.findall(strip_markup(value).lower()))
