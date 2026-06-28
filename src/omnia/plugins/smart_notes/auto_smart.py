"""Auto-smart: let the LLM infer a generation prompt + type for each field.

The user designates a base (input) field on a note type and enables the fields they want
generated. Rather than hand-writing a prompt per field, "auto-smart" asks an LLM — primed as
a senior language master — to infer, from each field's NAME plus the base field, what the
field should contain, pick the right generation type (a "Pronunciation"/"Audio" field → tts;
an "Image"/"Picture" field → image; "Meaning"/"Definition"/"Example"/"IPA" → text), and write
a concise prompt template that references ``{{<base>}}`` (and may reference other fields).

The prompt-building and result-applying are PURE and unit-tested; only :func:`generate_auto_smart`
touches a provider (via the injected :class:`~omnia.core.providers.ProviderHub`). Provider or
parse failures raise :class:`~omnia.core.providers.errors.ProviderError`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    from omnia.core.config.models import (
        SmartNotesFieldConfig,
        SmartNotesNoteTypeConfig,
    )
    from omnia.core.providers import ProviderHub

_PERSONA = (
    "You are a senior language master fluent in many languages, helping an Anki learner "
    "automate flashcard creation so they avoid typing prompts by hand."
)

_VALID_TYPES = {"text", "image", "tts"}

# The first {...} object in the model's reply (tolerates code fences / surrounding prose).
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class AutoSmartField:
    """The LLM's suggestion for one field: its generation ``type`` + ``prompt`` template."""

    type: str
    prompt: str


def build_auto_smart_prompt(
    note_type: str, base_field: str, field_names: list[str]
) -> str:
    """Build the structured instruction asking the LLM for a type + prompt per field.

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field (referenced as ``{{<base>}}``).
        field_names: The candidate field names (enabled, not locked, not the base field).

    Returns:
        A single prompt instructing the model to return a JSON object keyed by field name,
        each value ``{"type": "text|tts|image", "prompt": "<template>"}``.
    """
    fields_list = "\n".join(f"- {name}" for name in field_names)
    return (
        f"{_PERSONA}\n\n"
        f'The note type is "{note_type}". Its base (input) field is "{base_field}"; '
        f"reference it in templates as {{{{{base_field}}}}}.\n\n"
        "For EACH of the following target fields, infer from its name and the base field "
        "what it should contain, then choose:\n"
        '  - "type": one of "text", "tts", or "image". Use "tts" for audio/pronunciation '
        'fields, "image" for picture/illustration fields, and "text" otherwise '
        "(meaning, definition, example, IPA, translation, etc.).\n"
        '  - "prompt": a concise generation template that references '
        f"{{{{{base_field}}}}} (and may reference other fields by name), telling the model "
        "exactly what to produce for that field. Keep it one or two sentences.\n\n"
        f"Target fields:\n{fields_list}\n\n"
        "Respond with ONLY a JSON object mapping each field name to "
        '{"type": ..., "prompt": ...}. No prose, no code fences.'
    )


def parse_auto_smart_response(raw: str) -> dict[str, AutoSmartField]:
    """Parse the LLM reply into per-field suggestions, tolerating fences / extra prose.

    Extracts the first ``{...}`` JSON object from ``raw`` (so code fences or surrounding
    commentary don't break parsing), then reads each field's ``type``/``prompt``. An invalid
    ``type`` falls back to ``"text"``; a missing ``prompt`` falls back to an empty string.

    Args:
        raw: The model's raw text reply.

    Returns:
        A mapping of field name → :class:`AutoSmartField`.

    Raises:
        ProviderError: When no JSON object can be extracted or it is not an object.
    """
    match = _JSON_OBJECT_RE.search(raw or "")
    if match is None:
        raise ProviderError(
            "auto-smart: the model reply contained no JSON object to parse"
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"auto-smart: could not parse the model's JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ProviderError("auto-smart: the model reply was not a JSON object")

    suggestions: dict[str, AutoSmartField] = {}
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        field_type = str(value.get("type", "text")).strip().lower()
        if field_type not in _VALID_TYPES:
            field_type = "text"
        prompt = str(value.get("prompt", "") or "")
        suggestions[str(name)] = AutoSmartField(type=field_type, prompt=prompt)
    return suggestions


def apply_auto_smart(
    config: SmartNotesNoteTypeConfig, suggestions: dict[str, AutoSmartField]
) -> SmartNotesNoteTypeConfig:
    """Return ``config`` updated with the auto-smart ``suggestions``.

    ONLY enabled, non-locked fields get their ``type``/``prompt`` overwritten, and only when
    the model returned a suggestion for them. Locked fields, disabled fields, fields with no
    suggestion, and the base field are left untouched.

    Args:
        config: The current note-type config.
        suggestions: Per-field suggestions from :func:`parse_auto_smart_response`.

    Returns:
        A new :class:`~omnia.core.config.models.SmartNotesNoteTypeConfig` (the input is not
        mutated).
    """
    updated: list[SmartNotesFieldConfig] = []
    for field in config.fields:
        suggestion = suggestions.get(field.field)
        if (
            suggestion is not None
            and field.enabled
            and not field.prompt_locked
            and field.field != config.base_field
        ):
            updated.append(
                field.copy(
                    update={"type": suggestion.type, "prompt": suggestion.prompt}
                )
            )
        else:
            updated.append(field.copy())
    return config.copy(update={"fields": updated})


def candidate_fields(config: SmartNotesNoteTypeConfig) -> list[str]:
    """Return the field names auto-smart may rewrite: enabled, not locked, not the base."""
    return [
        field.field for field in config.generatable_fields() if not field.prompt_locked
    ]


def generate_auto_smart(
    hub: ProviderHub, config: SmartNotesNoteTypeConfig
) -> SmartNotesNoteTypeConfig:
    """Ask the LLM to fill in prompts/types for ``config``'s candidate fields, then apply them.

    Thin glue: gathers the candidate fields (enabled, not locked, not the base), builds the
    structured prompt, calls the active LLM via ``hub``, parses the JSON reply, and applies it
    to a copy of ``config``. A no-op (returns ``config`` unchanged) when there are no
    candidate fields.

    Args:
        hub: The provider hub (the active LLM is used; no per-field override here).
        config: The note-type config to fill in.

    Returns:
        An updated config with prompts/types for the candidate fields.

    Raises:
        ProviderError: On a provider failure or an unparseable reply.
    """
    candidates = candidate_fields(config)
    if not candidates:
        return config
    prompt = build_auto_smart_prompt(config.note_type, config.base_field, candidates)
    raw = hub.llm().generate_text(prompt)
    suggestions = parse_auto_smart_response(raw)
    return apply_auto_smart(config, suggestions)
