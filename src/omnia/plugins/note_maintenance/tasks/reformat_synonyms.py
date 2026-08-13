"""Pair a trailing IPA group back onto the synonyms it belongs to.

Dictionary exports often collapse a synonym list into ``"modest, meek (ˈmɒdɪst, miːk)"`` —
all the words first, all their transcriptions afterwards. This task re-pairs them into
``"modest (ˈmɒdɪst), meek (miːk)"``, which is what a card can actually display one line at a
time.

With ``strict_count_match`` on (the default) a list whose two halves have different lengths is
left ALONE: the pairing would be a guess, and a wrong transcription is worse than none.

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

# "<words> (<transcriptions>)" — the whole field, with one trailing parenthesised group.
_SPLIT_RE = re.compile(r"^(.*?)\s*\((.*?)\)\s*$")


class ReformatSynonymsConfig(TaskConfigBase):
    """Which field to re-pair, and how strict the pairing has to be."""

    field: str = Field(
        "Synonyms",
        title="Field",
        description="The field holding the synonym list (rewritten in place).",
    )
    strict_count_match: bool = Field(
        True,
        title="Only pair when the counts match",
        description=(
            "Require as many transcriptions as words. Off = pair as many as line up and drop "
            "the rest, which can attach the WRONG transcription to a word."
        ),
    )


@register_task("reformat_synonyms")
class ReformatSynonymsTask(MaintenanceTask):
    """Rewrites ``"a, b (ipa-a, ipa-b)"`` as ``"a (ipa-a), b (ipa-b)"``."""

    name = "Reformat synonyms"
    description = "Pair a trailing group of transcriptions back onto their synonyms."
    config_model = ReformatSynonymsConfig

    def process(self, note: NoteView) -> dict[str, str]:
        field = self.config.field
        raw = note.field(field).strip()
        match = _SPLIT_RE.match(raw) if raw else None
        if match is None:
            return {}
        left, right = match.group(1), match.group(2)
        # An ALREADY paired list ("a (x), b (y)") also matches, with the inner brackets landing
        # inside the groups. Re-pairing it would corrupt it, so leave anything bracketed alone.
        if "(" in left or "(" in right or ")" in right:
            return {}
        words = [part.strip() for part in left.split(",") if part.strip()]
        transcriptions = [part.strip() for part in right.split(",") if part.strip()]
        if not (words and transcriptions):
            return {}
        if self.config.strict_count_match and len(words) != len(transcriptions):
            return {}
        # strict=False: with the count check off, the extra words/transcriptions are dropped.
        paired = ", ".join(
            f"{word} ({ipa})" for word, ipa in zip(words, transcriptions, strict=False)
        )
        return {field: paired} if paired != raw else {}
