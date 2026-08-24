"""Prompt placeholder interpolation for smart-notes field rules.

Pure logic — no Anki imports. A field's prompt template references the note's other fields as
``{{FieldName}}`` placeholders; this module extracts those references and substitutes their
values, while deliberately leaving Anki cloze deletions (``{{c1::...}}``) untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnia.core.providers.llm.base import PromptParts

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")


# A cloze deletion opener ({{c1::...) — NOT a field ref, mirroring _FIELD_RE's lookahead.
_CLOZE_RE = re.compile(r"\{\{c\d+::")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def rename_field_refs(prompt: str, renames: Mapping[str, str]) -> str:
    """Rewrite ``{{Old}}`` placeholders to ``{{New}}`` per ``renames``, leaving the rest alone.

    Lives beside :func:`extract_field_refs` so both read the SAME placeholder definition: a
    rename that used its own regex would eventually disagree about what counts as a reference
    and silently rewrite an Anki cloze deletion (``{{c1::…}}``) or miss a spaced ``{{ Field }}``.

    A prompt is one of the places a field name is written down — the others being the rule's
    own name, ``base_field``, ``depends_on``, ``node_positions`` and a tool's params. Renaming
    a field without rewriting its prompts leaves references pointing at a field that no longer
    exists, which reads as "generation quietly stopped using my sentence".

    Args:
        prompt: The prompt template.
        renames: ``{old field name: new field name}``. Names not present are left as they are.

    Returns:
        The prompt with its field references rewritten (whitespace inside the braces is
        normalised away for a ref that IS renamed, and preserved for one that is not).
    """
    if not prompt or not renames:
        return prompt

    def _swap(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        new = renames.get(name)
        return match.group(0) if new is None else "{{" + new + "}}"

    return _FIELD_RE.sub(_swap, prompt)


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


def split_prompt(prompt: str, fields: dict[str, str]) -> PromptParts:
    """Interpolate ``prompt``, split at its FIRST ``{{ref}}``: literal head, then the rest.

    Lossless — ``prefix + suffix`` is byte-for-byte what :func:`interpolate` returns — so this
    cannot change a single generation's output. All it does is stop the template's leading
    instructions from being buried behind a substituted value, which is the whole of what a
    provider prefix cache needs: every note of a note type then sends the same head.

    Deliberately conservative. Restructuring the prompt into "instructions with the refs left
    uninterpolated" plus a values block would maximise the cacheable prefix, but it changes the
    string the model sees, and therefore the output, for every existing user on every field. A
    template that LEADS with ``{{Word}}`` gets an empty prefix and no benefit; that is the
    accepted cost of never touching what the model reads.

    Args:
        prompt: The prompt template.
        fields: The note's field values.

    Returns:
        The interpolated prompt, split into its cacheable head and the rest.
    """
    match = _FIELD_RE.search(prompt)
    if match is None:
        # No refs at all: the whole prompt is literal, so all of it is cacheable and there is
        # nothing to interpolate.
        return PromptParts(prompt, "")
    head = prompt[: match.start()]
    return PromptParts(head, interpolate(prompt[match.start() :], fields))
