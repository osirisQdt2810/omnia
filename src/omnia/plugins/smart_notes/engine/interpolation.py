"""Prompt placeholder interpolation for smart-notes field rules.

Pure logic — no Anki imports. A field's prompt template references the note's other fields as
``{{FieldName}}`` placeholders; this module extracts those references and substitutes their
values, while deliberately leaving Anki cloze deletions (``{{c1::...}}``) untouched.
"""

from __future__ import annotations

import re

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)
