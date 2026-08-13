"""Render a :class:`~omnia.plugins.note_maintenance.runner.NoteChange` as an inline HTML diff.

A batch edit is destructive, so the user has to SEE what a task would do before confirming.
This module turns each changed field into one row carrying the old and the new text with the
differing runs marked (``<del>`` / ``<ins>``), which the preview dialog drops straight into its
webview.

The comparison is word-level rather than character-level: a note field is prose, and a
character diff of a sentence reads as confetti. Values are HTML-escaped, so the markup a field
CONTAINS is shown as text and can never leak into the dialog's own markup.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from omnia.plugins.note_maintenance.runner import FieldChange, NoteChange

# Split into words and the whitespace between them, so re-joining restores the text exactly.
_TOKEN_RE = re.compile(r"\s+|\S+")


@dataclass(frozen=True)
class DiffRow:
    """One field's diff: the old and new text, with the differing runs marked."""

    field: str
    before_html: str
    after_html: str

    @classmethod
    def build(cls, change: FieldChange) -> Optional[DiffRow]:
        """Return the row for ``change``, or None when the value did not actually change.

        Args:
            change: The field's before/after values.

        Returns:
            A :class:`DiffRow` with ``<del>``/``<ins>`` marks, or None for an unchanged field.
        """
        if change.before == change.after:
            return None
        before_tokens = _TOKEN_RE.findall(change.before)
        after_tokens = _TOKEN_RE.findall(change.after)
        matcher = SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)
        before_parts: list[str] = []
        after_parts: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            removed = _escape(before_tokens[i1:i2])
            added = _escape(after_tokens[j1:j2])
            if tag == "equal":
                before_parts.append(removed)
                after_parts.append(added)
                continue
            if removed:
                before_parts.append(f"<del>{removed}</del>")
            if added:
                after_parts.append(f"<ins>{added}</ins>")
        return cls(
            field=change.field,
            before_html="".join(before_parts),
            after_html="".join(after_parts),
        )


class NoteDiff:
    """The renderable diff of one note's pending changes."""

    def __init__(self, change: NoteChange) -> None:
        """Initialise the diff.

        Args:
            change: The note's pending changes, as produced by the runner.
        """
        self._change = change

    @property
    def note_id(self) -> int:
        """The id of the note being diffed."""
        return self._change.note_id

    def rows(self) -> list[DiffRow]:
        """Return one :class:`DiffRow` per genuinely changed field (unchanged fields drop out)."""
        rows = (DiffRow.build(change) for change in self._change.fields)
        return [row for row in rows if row is not None]


def _escape(tokens: list[str]) -> str:
    """Join ``tokens`` back into HTML-escaped text (a field's own markup shows as text)."""
    return html.escape("".join(tokens))
