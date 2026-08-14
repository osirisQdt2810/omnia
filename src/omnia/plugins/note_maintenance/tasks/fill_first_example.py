"""Refill a plain example sentence from its clozed twin when the two have drifted apart.

Two fields hold the same sentence — one plain, one with the target word clozed. Editing only
the clozed one (the usual direction: that is the field being reviewed) leaves the plain field
stale. This task copies the clozed field over when the two no longer say the same thing.

Both the comparison and the copy go through :func:`~omnia.core.lang.text.strip_markup` (which
unwraps a cloze deletion to its answer):

* "the same thing" is measured on the WORDS, not the markup — Jaccard similarity over the
  stripped text, so ``<b>`` tags, ``{{c1::…}}`` wrappers and whitespace never count as a
  difference;
* the refill writes the STRIPPED sentence, because the target is the plain twin — a field
  holding ``{{c1::…}}`` markup no cloze template renders is not a refill, it is a mess.

Stripped text is then re-encoded as field HTML before it is written (see
:func:`~omnia.core.lang.text.as_field_html`): the target is a STORED-HTML field, not a plain-text one.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re

from pydantic import Field

from omnia.core.lang.text import as_field_html, strip_markup
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    OptionKind,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*|\d+", re.UNICODE)


class FillFirstExampleConfig(TaskConfigBase):
    """Which fields hold the two copies of the sentence, and how different is too different."""

    order: int = Field(
        40,
        title="Run order",
        description="Independent of the other bundled tasks; runs after them by convention.",
    )
    source_field: str = Field(
        "Clozed First Example",
        title="Source field",
        description="The field that is kept up to date (usually the clozed sentence).",
        renders_as=OptionKind.NOTE_FIELD,
    )
    target_field: str = Field(
        "First Example",
        title="Target field",
        description=(
            "The field refilled when the two have drifted apart. It receives the source as "
            "PLAIN text — markup stripped and cloze deletions unwrapped to their answer, with "
            "the author's line breaks kept as <br>."
        ),
        renders_as=OptionKind.NOTE_FIELD,
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
        # The target is the PLAIN twin, so it gets the sentence as text: copying the raw source
        # would put "{{c1::plunged}}" on a field no cloze template ever renders. Markup that
        # carried no words at all (a lone [sound:…]) would empty the field — leave it be.
        plain = strip_markup(source)
        return {self.config.target_field: as_field_html(plain)} if plain else {}

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
