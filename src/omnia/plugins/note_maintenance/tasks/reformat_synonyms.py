"""Pair a trailing IPA group back onto the synonyms it belongs to.

Dictionary exports often collapse a synonym list into ``"modest, meek (ˈmɒdɪst, miːk)"`` —
all the words first, all their transcriptions afterwards. This task re-pairs them into
``"modest (ˈmɒdɪst), meek (miːk)"``, which is what a card can actually display one line at a
time.

With ``strict_count_match`` on (the default) a list whose two halves have different lengths is
left ALONE: the pairing would be a guess, and a wrong transcription is worse than none. With it
off, the halves are paired as far as they line up and everything left over is kept VERBATIM —
this is an in-place rewrite, so a word or a transcription this task cannot place must still
survive it.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re

from pydantic import Field

from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    OptionKind,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task

# "<words> (<transcriptions>)" — the whole field, with one trailing parenthesised group.
_SPLIT_RE = re.compile(r"^(.*?)\s*\((.*?)\)\s*$")


class ReformatSynonymsConfig(TaskConfigBase):
    """Which field to re-pair, and how strict the pairing has to be."""

    order: int = Field(
        10,
        title="Run order",
        description=(
            "Runs BEFORE strip_ipa (20): strip_ipa only understands an already paired list, "
            "so the other way round a collapsed list would need two runs to settle."
        ),
    )
    field: str = Field(
        "Synonyms",
        title="Field",
        description="The field holding the synonym list (rewritten in place).",
        renders_as=OptionKind.NOTE_FIELD,
    )
    strict_count_match: bool = Field(
        True,
        title="Only pair when the counts match",
        description=(
            "Require as many transcriptions as words. Off = pair as many as line up and keep "
            "the leftovers unpaired (nothing is deleted), which can attach the WRONG "
            "transcription to a word."
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
        paired = self._pair(words, transcriptions)
        return {field: paired} if paired != raw else {}

    @staticmethod
    def _pair(words: list[str], transcriptions: list[str]) -> str:
        """Return the re-paired list, keeping whatever could not be paired.

        The halves are zipped as far as they line up. With uneven halves (only reachable with
        ``strict_count_match`` off) the surplus is preserved rather than dropped: a spare word
        is emitted bare, and spare transcriptions stay in a trailing group of their own. This
        rewrite replaces the field, so anything dropped here is deleted from the collection.
        """
        segments = [
            f"{word} ({ipa})" for word, ipa in zip(words, transcriptions, strict=False)
        ]
        matched = len(segments)
        segments.extend(words[matched:])
        spare = transcriptions[matched:]
        if spare:
            segments.append(f"({', '.join(spare)})")
        return ", ".join(segments)
