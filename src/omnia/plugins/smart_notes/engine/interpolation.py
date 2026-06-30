"""Prompt placeholder interpolation for smart-notes field rules.

Pure logic — no Anki imports. A field's prompt template references the note's other fields as
``{{FieldName}}`` placeholders; this module extracts those references and substitutes their
values, while deliberately leaving Anki cloze deletions (``{{c1::...}}``) untouched.
"""

from __future__ import annotations

import re

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")


# A cloze deletion opener ({{c1::...) — NOT a field ref, mirroring _FIELD_RE's lookahead.
_CLOZE_RE = re.compile(r"\{\{c\d+::")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def validate_brace_syntax(prompt: str) -> list[str]:
    """Return human-readable errors for malformed ``{{Field}}`` placeholders in ``prompt``.

    A token scan over ``{{`` / ``}}`` markers that reports:

    * an unclosed ``{{`` (a ``{{`` with no following ``}}``);
    * a stray ``}}`` (a ``}}`` with no preceding ``{{``);
    * an empty placeholder (``{{}}`` / ``{{ }}`` — braces with no field name).

    Anki cloze deletions (``{{c1::...}}``) are NOT field refs (mirroring the negative lookahead
    :data:`_FIELD_RE` uses) and are skipped — their braces are never flagged.

    Args:
        prompt: The prompt template to validate.

    Returns:
        A list of error messages (empty when the braces are well-formed).
    """
    errors: list[str] = []
    index = 0
    length = len(prompt)
    while index < length:
        open_at = prompt.find("{{", index)
        if open_at == -1:
            break
        if _CLOZE_RE.match(prompt, open_at):
            # A cloze opener: skip to its closing }} (or end) without validating it as a ref.
            close_at = prompt.find("}}", open_at + 2)
            index = length if close_at == -1 else close_at + 2
            continue
        close_at = prompt.find("}}", open_at + 2)
        if close_at == -1:
            errors.append("Unclosed '{{' — every '{{' needs a matching '}}'.")
            break
        # The inner text may itself contain stray '{{' (e.g. "{{Wo{{rd}}"); take the LAST '{{'
        # before this '}}' as the real opener so the leading stray brace is reported separately.
        inner_open = prompt.rfind("{{", open_at + 2, close_at)
        real_open = inner_open if inner_open != -1 else open_at
        if inner_open != -1:
            errors.append("Unclosed '{{' — every '{{' needs a matching '}}'.")
        if not prompt[real_open + 2 : close_at].strip():
            errors.append(
                "Empty placeholder '{{}}' — name the field inside the braces."
            )
        index = close_at + 2
    # A stray '}}' with no preceding '{{' (scan the parts outside the placeholders we matched).
    if _has_unopened_close(prompt):
        errors.append("Stray '}}' — a '}}' has no matching '{{'.")
    return errors


def _has_unopened_close(prompt: str) -> bool:
    """Whether ``prompt`` contains a ``}}`` that no preceding ``{{`` opened."""
    depth = 0
    index = 0
    length = len(prompt)
    while index < length:
        next_open = prompt.find("{{", index)
        next_close = prompt.find("}}", index)
        if next_close == -1:
            return False
        if next_open != -1 and next_open < next_close:
            depth += 1
            index = next_open + 2
            continue
        if depth == 0:
            return True
        depth -= 1
        index = next_close + 2
    return False


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)
