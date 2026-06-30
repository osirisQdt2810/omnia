"""Prompt authoring: infer field prompts/types (auto-smart) and refine rough prompts.

Users either let the LLM infer a generation prompt + type per field ("auto-smart") or type a
short, rough description that the LLM rewrites into a polished, self-guarding, Anki-optimised
prompt ("improve"). The message-building and reply-parsing are PURE and unit-tested;
:class:`PromptAuthor` is the thin object that wraps an injected ``LLMProvider`` (DIP) and
turns those pure pieces into the three authoring actions. This module imports nothing from
``aqt``/``anki``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnia.core import envs
from omnia.plugins.smart_notes.authoring.models import AutoSmartDep, AutoSmartField
from omnia.plugins.smart_notes.authoring.persona import (
    FLASHCARD_EXPERT_SYSTEM,
    first_json_object,
)

if TYPE_CHECKING:
    from omnia.core.providers.llm.base import LLMProvider
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldConfig,
        SmartNotesNoteTypeConfig,
    )

_VALID_TYPES = {"text", "image", "tts"}


# ---------------------------------------------------------------------------
# Auto-smart: pure prompt-build, reply-parse, and result-apply.
# ---------------------------------------------------------------------------


def build_auto_smart_prompt(
    note_type: str,
    base_field: str,
    field_names: list[str],
    *,
    existing_deps: dict[str, list[AutoSmartDep]] | None = None,
) -> str:
    """Build the structured instruction asking the LLM for a type + prompt per field.

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field (referenced as ``{{<base>}}``).
        field_names: The candidate field names (enabled, not locked, not the base field).
        existing_deps: The dependency edges already drawn for each field
            (``{field: [AutoSmartDep, ...]}``). When non-empty the model is told to KEEP these
            edges and only add missing ones; when empty/absent it proposes the graph fresh.

    Returns:
        A single prompt instructing the model to return a JSON object keyed by field name,
        each value ``{"type": "text|tts|image", "prompt": "<template>",
        "depends_on": [{"field": "<FieldName>", "kind": "hard|soft"}]}``.
    """
    fields_list = "\n".join(f"- {name}" for name in field_names)
    return (
        "You are a senior language master automating Anki flashcard creation for a learner "
        "who does not want to hand-write a prompt per field.\n\n"
        f'The note type is "{note_type}". Its base (input) field is "{base_field}"; '
        f"reference it in templates as {{{{{base_field}}}}}.\n\n"
        "For EACH target field below, infer from its name and the base field what it should "
        "contain, then choose:\n"
        '  - "type": one of "text", "tts", or "image". Use "tts" for audio/pronunciation '
        'fields, "image" for picture/illustration fields, and "text" otherwise '
        "(meaning, definition, example, IPA, translation, etc.).\n"
        '  - "prompt": a COMPLETE, production-grade generation template (not a one-liner) '
        f"that references {{{{{base_field}}}}} (and other fields by name where useful), "
        "self-guards when a referenced field may be empty, and pins down concise, "
        "Anki-friendly output — exactly to the standard set in the system message.\n"
        '  - "depends_on": a list of the OTHER fields this field needs, each '
        '{"field": "<FieldName>", "kind": "hard"|"soft"}. A "hard" dependency means that '
        'field\'s content is required to generate this one; a "soft" dependency is helpful '
        "optional context. Only reference fields that exist (the base field or another target "
        "field), and NEVER create a cycle (no field may depend, directly or transitively, on "
        "itself).\n\n"
        f"Target fields:\n{fields_list}\n\n"
        f"{_existing_deps_block(existing_deps)}"
        "Respond with ONLY a JSON object mapping each field name to "
        '{"type": ..., "prompt": ..., "depends_on": [...]}. No prose, no code fences.'
    )


def _existing_deps_block(existing_deps: dict[str, list[AutoSmartDep]] | None) -> str:
    """Render the KEEP-these-edges block, or an empty string when no edges exist yet.

    Serializes the current dependency graph so the model respects user-drawn edges instead of
    clobbering them: it is told to keep every listed edge and only add the missing ones.
    """
    if not existing_deps:
        return ""
    lines: list[str] = []
    for field, deps in existing_deps.items():
        for dep in deps:
            lines.append(f'  - "{field}" depends on "{dep.field}" ({dep.kind})')
    if not lines:
        return ""
    listing = "\n".join(lines)
    return (
        "The dependency graph ALREADY has these edges — KEEP them exactly and only ADD any "
        f"missing dependencies (do not drop or change these):\n{listing}\n\n"
    )


def parse_auto_smart_response(raw: str) -> dict[str, AutoSmartField]:
    """Parse the LLM reply into per-field suggestions, tolerating fences / extra prose.

    Extracts the first ``{...}`` JSON object from ``raw`` (so code fences or surrounding
    commentary don't break parsing), then reads each field's ``type``/``prompt``/``depends_on``.
    An invalid ``type`` falls back to ``"text"``; a missing ``prompt`` falls back to an empty
    string; ``depends_on`` is parsed defensively (see :func:`_parse_deps`). No cycle validation
    happens here — the engine owns that.

    Args:
        raw: The model's raw text reply.

    Returns:
        A mapping of field name → :class:`AutoSmartField`.

    Raises:
        ProviderError: When no JSON object can be extracted or it is not an object.
    """
    data = first_json_object(raw)
    suggestions: dict[str, AutoSmartField] = {}
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        field_type = str(value.get("type", "text")).strip().lower()
        if field_type not in _VALID_TYPES:
            field_type = "text"
        prompt = str(value.get("prompt", "") or "")
        suggestions[str(name)] = AutoSmartField(
            type=field_type,
            prompt=prompt,
            depends_on=_parse_deps(str(name), value.get("depends_on")),
        )
    return suggestions


def _parse_deps(owner: str, value: object) -> tuple[AutoSmartDep, ...]:
    """Read one field's ``depends_on`` list into ``AutoSmartDep`` entries, tolerantly.

    Each entry needs a non-empty ``field`` (else it is dropped); ``kind`` defaults to ``"hard"``
    and anything other than ``"soft"`` becomes ``"hard"``; a self-reference (``owner`` naming
    itself) is ignored. A non-list ``value`` yields no edges.
    """
    if not isinstance(value, list):
        return ()
    deps: list[AutoSmartDep] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        field = str(entry.get("field", "") or "").strip()
        if not field or field.lower() == owner.strip().lower():
            continue
        kind = (
            "soft"
            if str(entry.get("kind", "hard")).strip().lower() == "soft"
            else "hard"
        )
        deps.append(AutoSmartDep(field=field, kind=kind))
    return tuple(deps)


def apply_auto_smart(
    config: SmartNotesNoteTypeConfig, suggestions: dict[str, AutoSmartField]
) -> SmartNotesNoteTypeConfig:
    """Return ``config`` updated with the auto-smart ``suggestions``.

    ONLY enabled, non-locked fields get their ``type``/``prompt`` overwritten, and only when
    the model returned a suggestion for them. Locked fields, disabled fields, fields with no
    suggestion, and the base field are left untouched. The suggested ``depends_on`` fills the
    graph only where a field has NO explicit ``depends_on`` yet — user-drawn/existing edges are
    preserved (fill gaps, don't clobber).

    Args:
        config: The current note-type config.
        suggestions: Per-field suggestions from :func:`parse_auto_smart_response`.

    Returns:
        A new :class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` (the input is not
        mutated).
    """
    from omnia.plugins.smart_notes.config import FieldDep

    updated: list[SmartNotesFieldConfig] = []
    for field in config.fields:
        suggestion = suggestions.get(field.field)
        if (
            suggestion is not None
            and field.enabled
            and not field.prompt_locked
            and field.field != config.base_field
        ):
            change: dict[str, object] = {
                "type": suggestion.type,
                "prompt": suggestion.prompt,
            }
            # Fill the dependency graph only for a field that has no explicit edges yet —
            # never clobber user-drawn/existing ones.
            if not field.depends_on and suggestion.depends_on:
                change["depends_on"] = [
                    FieldDep(field=dep.field, kind=dep.kind)
                    for dep in suggestion.depends_on
                ]
            updated.append(field.copy(update=change))
        else:
            updated.append(field.copy())
    return config.copy(update={"fields": updated})


def candidate_fields(config: SmartNotesNoteTypeConfig) -> list[str]:
    """Return the field names auto-smart may rewrite: enabled, not locked, not the base."""
    return [
        field.field for field in config.generatable_fields() if not field.prompt_locked
    ]


def existing_deps(config: SmartNotesNoteTypeConfig) -> dict[str, list[AutoSmartDep]]:
    """The dependency edges already drawn per field, for seeding the auto-smart prompt.

    Only fields that actually carry explicit ``depends_on`` appear, so an empty graph yields an
    empty map (the model then proposes the graph fresh).
    """
    result: dict[str, list[AutoSmartDep]] = {}
    for field in config.fields:
        if field.depends_on:
            result[field.field] = [
                AutoSmartDep(field=dep.field, kind=dep.kind) for dep in field.depends_on
            ]
    return result


# ---------------------------------------------------------------------------
# Improve: pure message-build + reply-parse for rough-prompt refinement.
# ---------------------------------------------------------------------------


def _field_ref_list(other_fields: list[str]) -> str:
    """A comma list of ``{{Field}}`` references, or a hint when there are none."""
    refs = [f"{{{{{name}}}}}" for name in other_fields if name]
    return ", ".join(refs) if refs else "(no other fields available)"


def build_improve_prompt_message(
    note_type: str,
    base_field: str,
    target_field: str,
    rough: str,
    other_fields: list[str],
) -> str:
    """Build the user message asking the model to rewrite ONE rough prompt.

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field, referenced as ``{{<base>}}``.
        target_field: The field this prompt will generate.
        rough: The user's short/rough description of what they want for the field.
        other_fields: Field names available to reference (excluding the target).

    Returns:
        A single user message (the persona/rules live in
        :data:`~omnia.plugins.smart_notes.authoring.persona.FLASHCARD_EXPERT_SYSTEM`).
    """
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}. '
        f'Field to generate: "{target_field}".\n'
        f"Other fields you may reference: {_field_ref_list(other_fields)}.\n\n"
        "The user's rough request for this field:\n"
        f'"""\n{rough.strip()}\n"""\n\n'
        "Rewrite it into ONE complete, production-grade generation prompt that follows your "
        "rules. Decide for yourself which fields the prompt should reference and how to "
        "self-guard when they are empty. Output ONLY the prompt text."
    )


def build_improve_prompts_message(
    note_type: str, base_field: str, items: list[tuple[str, str]]
) -> str:
    """Build the user message rewriting MANY rough prompts at once (the global action).

    Args:
        note_type: The note type's name.
        base_field: The base input field, referenced as ``{{<base>}}``.
        items: ``(field_name, rough_prompt)`` pairs for the fields to improve.

    Returns:
        A user message instructing a JSON object keyed by field name → improved prompt.
    """
    listing = "\n".join(
        f'- "{field}": current request = """{rough.strip()}"""'
        for field, rough in items
    )
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}.\n'
        "Rewrite EACH of the following fields' rough requests into a complete, "
        "production-grade generation prompt that follows your rules:\n\n"
        f"{listing}\n\n"
        "Respond with ONLY a JSON object mapping each field name to its rewritten prompt "
        "string. No prose, no code fences."
    )


def parse_improved_prompts(raw: str) -> dict[str, str]:
    """Parse the global-improve reply into a ``{field: improved_prompt}`` map.

    Non-string values are dropped (a defensive parse), so a partly-malformed reply still
    yields the usable entries instead of raising.
    """
    data = first_json_object(raw)
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


# ---------------------------------------------------------------------------
# PromptAuthor: the LLM-backed authoring object (DIP).
# ---------------------------------------------------------------------------


class PromptAuthor:
    """Authors/refines field-generation prompts via an injected LLMProvider (DIP).

    Wraps the pure builders/parsers above so the three authoring actions — infer prompts for a
    whole note type (:meth:`auto_smart`), rewrite one rough prompt (:meth:`improve`), and
    rewrite many at once (:meth:`improve_all`) — share one LLM and one persona. Provider or
    parse failures raise :class:`~omnia.core.providers.errors.ProviderError`.
    """

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def auto_smart(self, config: SmartNotesNoteTypeConfig) -> SmartNotesNoteTypeConfig:
        """Infer prompts/types for ``config``'s candidate fields, then apply them.

        Gathers the candidate fields (enabled, not locked, not the base), builds the structured
        prompt, calls the LLM, parses the JSON reply, and applies it to a copy of ``config``. A
        no-op (returns ``config`` unchanged) when there are no candidate fields.

        Raises:
            ProviderError: On a provider failure or an unparseable reply.
        """
        candidates = candidate_fields(config)
        if not candidates:
            return config
        prompt = build_auto_smart_prompt(
            config.note_type,
            config.base_field,
            candidates,
            existing_deps=existing_deps(config),
        )
        raw = self._llm.generate_text(
            prompt,
            system=FLASHCARD_EXPERT_SYSTEM,
            temperature=envs.OMNIA_SMART_NOTES_AUTO_PROMPT_TEMPERATURE,
        )
        return apply_auto_smart(config, parse_auto_smart_response(raw))

    def improve(
        self,
        *,
        note_type: str,
        base_field: str,
        target_field: str,
        rough: str,
        other_fields: list[str],
    ) -> str:
        """Rewrite one field's rough prompt into a polished one (best result text).

        Returns the original ``rough`` text unchanged when it is blank (nothing to improve).

        Raises:
            ProviderError: On a provider/network failure.
        """
        if not rough.strip():
            return rough
        message = build_improve_prompt_message(
            note_type, base_field, target_field, rough, other_fields
        )
        out = self._llm.generate_text(
            message,
            system=FLASHCARD_EXPERT_SYSTEM,
            temperature=envs.OMNIA_SMART_NOTES_IMPROVE_PROMPT_TEMPERATURE,
        )
        return out.strip() or rough

    def improve_all(
        self,
        *,
        note_type: str,
        base_field: str,
        items: list[tuple[str, str]],
    ) -> dict[str, str]:
        """Rewrite many fields' rough prompts at once; return ``{field: improved_prompt}``.

        A no-op (returns ``{}``) when there are no items with a non-blank prompt.

        Raises:
            ProviderError: On a provider/network failure or an unparseable reply.
        """
        pending = [(field, rough) for field, rough in items if rough.strip()]
        if not pending:
            return {}
        message = build_improve_prompts_message(note_type, base_field, pending)
        raw = self._llm.generate_text(
            message,
            system=FLASHCARD_EXPERT_SYSTEM,
            temperature=envs.OMNIA_SMART_NOTES_IMPROVE_ALL_TEMPERATURE,
        )
        return parse_improved_prompts(raw)
