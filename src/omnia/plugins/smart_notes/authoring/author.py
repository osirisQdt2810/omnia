"""Prompt authoring: infer field prompts/types (auto-smart) and refine rough prompts.

Users either let the LLM infer a generation prompt + type per field ("auto-smart") or type a
short, rough description that the LLM rewrites into a polished, self-guarding, Anki-optimised
prompt ("improve"). The message-building and reply-parsing are PURE and unit-tested;
:class:`PromptAuthor` is the thin object that wraps an injected ``LLMProvider`` (DIP) and
turns those pure pieces into the three authoring actions. This module imports nothing from
``aqt``/``anki``.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from omnia import envs
from omnia.plugins.smart_notes.authoring.models import (
    AutoSmartDep,
    AutoSmartField,
    EdgeChange,
    EdgeKinding,
    PromptRewrite,
)
from omnia.plugins.smart_notes.authoring.persona import (
    DEPENDENCY_CLASSIFIER_SYSTEM,
    FLASHCARD_EXPERT_SYSTEM,
    first_json_object,
)
from omnia.plugins.smart_notes.engine.consistency import NodeEdgeSet

if TYPE_CHECKING:
    from omnia.core.providers.llm.base import LLMProvider
    from omnia.plugins.smart_notes.config import (
        FieldDep,
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
    preserved (fill gaps, don't clobber) — AND only for deps the generated prompt actually
    references (``{{Field}}``). A suggested dep the prompt does not interpolate would be a
    dead edge (it orders/blocks but passes no value to the LLM), so it is dropped; every edge
    then reflects a real prompt reference.

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
            # Fill the dependency graph only for a field that has no explicit edges yet (never
            # clobber user-drawn/existing ones) AND only for deps the generated prompt actually
            # references — a suggested dep the prompt doesn't interpolate would be a dead edge
            # (orders/blocks but passes no value), so drop it. Every kept edge maps to a {{ref}}.
            if not field.depends_on and suggestion.depends_on:
                from omnia.plugins.smart_notes.engine.interpolation import (
                    extract_field_refs,
                )

                refs = {
                    ref.strip().lower()
                    for ref in extract_field_refs(suggestion.prompt)
                }
                kept = [
                    FieldDep(field=dep.field, kind=dep.kind)
                    for dep in suggestion.depends_on
                    if dep.field.strip().lower() in refs
                ]
                if kept:
                    change["depends_on"] = kept
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
# Two-way sync: dependency classification + edge-change / pinned rewrites.
# ---------------------------------------------------------------------------


def build_classify_deps_message(
    note_type: str,
    base_field: str,
    target_field: str,
    prompt: str,
    refs: list[str],
) -> str:
    """Build the message asking the model to label each referenced field hard/soft.

    Generic across note types: it names the note type, the base field, the field being
    generated, the prompt under inspection, and the referenced fields to classify, but bakes no
    note-specific rule into the wording (the hard/soft rubric lives in
    :data:`~omnia.plugins.smart_notes.authoring.persona.DEPENDENCY_CLASSIFIER_SYSTEM`).

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field, referenced as ``{{<base>}}``.
        target_field: The field this prompt generates.
        prompt: The prompt template whose references are being classified.
        refs: The referenced field names to classify (each labelled exactly once).

    Returns:
        A single user message asking for ``{"dependencies": [{"field", "kind", "reason"}]}``.
    """
    refs_list = "\n".join(f"- {name}" for name in refs)
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}. '
        f'Field being generated: "{target_field}".\n\n'
        "This field's generation prompt is:\n"
        f'"""\n{prompt.strip()}\n"""\n\n'
        "Classify how the generated field depends on EACH of these referenced fields "
        "(hard = its content fundamentally is-about / cannot exist without the reference; "
        "soft = the reference only sharpens an output already producible without it):\n"
        f"{refs_list}\n\n"
        'Respond with ONLY {"dependencies": [{"field": "<name>", "kind": "hard"|"soft", '
        '"reason": "<short>"}]}. No prose, no code fences.'
    )


def parse_classified_deps(raw: str) -> tuple[EdgeKinding, ...]:
    """Parse the classifier reply into per-ref hard/soft verdicts, tolerantly.

    Accepts either the ``{"dependencies": [...]}`` wrapper or a bare JSON array of entries.
    Mirrors :func:`_parse_deps`'s defensive style: an entry needs a non-empty ``field`` (else
    dropped); ``kind`` defaults to ``"hard"`` and anything other than ``"soft"`` becomes
    ``"hard"``; entries are de-duplicated case-insensitively (first wins). Self-references
    cannot be excluded here (no owner is known) — the caller passes only cross-field refs.

    Args:
        raw: The model's raw text reply.

    Returns:
        One :class:`EdgeKinding` per distinct referenced field.

    Raises:
        ProviderError: When no JSON object/array can be extracted from ``raw``.
    """
    return _parse_kindings(_classified_entries(raw))


def build_classify_deps_batch_message(
    note_type: str,
    base_field: str,
    items: list[tuple[str, str, list[str]]],
) -> str:
    """Build ONE message asking the model to classify many fields' refs in a single call.

    The cost mitigation for big note types (a note with dozens of fields must not fan out to one
    LLM call per field on Auto-prompt / Improve-all). Generic across note types — it bakes no
    note-specific rule into the wording (the hard/soft rubric lives in
    :data:`~omnia.plugins.smart_notes.authoring.persona.DEPENDENCY_CLASSIFIER_SYSTEM`).

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field, referenced as ``{{<base>}}``.
        items: ``(target_field, prompt, refs)`` triples — one per field to classify. ``refs`` is
            the referenced field names that field's prompt needs labelled.

    Returns:
        A single user message asking for a JSON object mapping each target field to its list of
        ``{"field", "kind"}`` verdicts.
    """
    blocks: list[str] = []
    for target_field, prompt, refs in items:
        refs_list = "\n".join(f"    - {name}" for name in refs)
        blocks.append(
            f'- Field "{target_field}", generation prompt:\n'
            f'  """\n{prompt.strip()}\n"""\n'
            f"  Referenced fields to classify:\n{refs_list}"
        )
    listing = "\n\n".join(blocks)
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}.\n\n'
        "For EACH field below, classify how the generated field depends on EACH of its "
        "referenced fields (hard = its content fundamentally is-about / cannot exist without "
        "the reference; soft = the reference only sharpens an output already producible without "
        f"it):\n\n{listing}\n\n"
        "Respond with ONLY a JSON object mapping each field name to its verdicts: "
        '{"<FieldName>": [{"field": "<name>", "kind": "hard"|"soft"}, ...], ...}. '
        "No prose, no code fences."
    )


def parse_classified_deps_batch(raw: str) -> dict[str, tuple[EdgeKinding, ...]]:
    """Parse the batch classifier reply into ``{field: (EdgeKinding, ...)}``, tolerantly.

    Reuses the single-reply per-entry tolerance (:func:`parse_classified_deps`'s rules) for each
    field's verdict list: an entry needs a non-empty ``field`` (else dropped); ``kind`` defaults
    to ``"hard"`` and anything other than ``"soft"`` becomes ``"hard"``; entries are
    de-duplicated case-insensitively (first wins). A map entry whose value is not a list is
    dropped, and a field key that strips to empty is dropped.

    Args:
        raw: The model's raw text reply.

    Returns:
        A mapping of target field name → its classified ``EdgeKinding`` tuple.

    Raises:
        ProviderError: When no JSON object can be extracted or it is not an object.
    """
    data = first_json_object(raw)
    result: dict[str, tuple[EdgeKinding, ...]] = {}
    for name, value in data.items():
        field = str(name).strip()
        if not field or not isinstance(value, list):
            continue
        result[field] = _parse_kindings(value)
    return result


def _parse_kindings(entries: list[object]) -> tuple[EdgeKinding, ...]:
    """Read one field's verdict list into de-duplicated ``EdgeKinding`` entries, tolerantly.

    The shared per-entry parse for both the single (:func:`parse_classified_deps`) and the batch
    (:func:`parse_classified_deps_batch`) classifier replies.
    """
    kindings: list[EdgeKinding] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field = str(entry.get("field", "") or "").strip()
        lower = field.lower()
        if not field or lower in seen:
            continue
        seen.add(lower)
        kind = (
            "soft"
            if str(entry.get("kind", "hard")).strip().lower() == "soft"
            else "hard"
        )
        reason = str(entry.get("reason", "") or "").strip()
        kindings.append(EdgeKinding(field=field, kind=kind, reason=reason))
    return tuple(kindings)


def _classified_entries(raw: str) -> list[object]:
    """Read the classifier reply's entry list from the wrapper key or a bare array.

    The shared :func:`first_json_object` extractor only yields ``{...}`` objects, so a bare
    ``[...]`` array reply is parsed here directly (with the same fence/prose tolerance) before
    falling back to the ``"dependencies"`` wrapper key.
    """
    array_match = re.search(r"\[.*\]", raw or "", re.DOTALL)
    object_match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    # Prefer a bare array only when it is not merely the wrapper object's inner list.
    if array_match and (
        object_match is None or array_match.start() < object_match.start()
    ):
        try:
            data = json.loads(array_match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return list(data)
    wrapper = first_json_object(raw)
    deps = wrapper.get("dependencies")
    return list(deps) if isinstance(deps, list) else []


def _deps_listing(deps: list[FieldDep]) -> str:
    """Render the KEEP-these-deps block for an edge-change / pinned rewrite message."""
    if not deps:
        return "(none)"
    return ", ".join(f"{{{{{dep.field}}}}} ({dep.kind})" for dep in deps)


def _synthetic_prompt(deps: list[FieldDep]) -> str:
    """A prompt of ``{{Field}}`` refs for each dep, so the intended edge set derives exactly it.

    The guard rail derives the INTENDED :class:`NodeEdgeSet` from this synthetic prompt UNIONed
    with ``deps``; referencing every dep's field guarantees the intended ref set is precisely the
    deps' fields, while ``deps`` overrides each edge's kind (so a soft dep yields a soft edge).
    """
    return " ".join(f"{{{{{dep.field}}}}}" for dep in deps)


def build_edge_change_message(
    note_type: str,
    base_field: str,
    target_field: str,
    old_prompt: str,
    kept_deps: list[FieldDep],
    change: EdgeChange,
) -> str:
    """Build the message rewriting a prompt to reflect ONE graph edge change.

    The model keeps every OTHER dependency intact and stays close to the old wording, applying
    only the single edit described by ``change``. The hard/soft/add/remove intuitions are given
    GENERICALLY (reasoning shapes, with cross-domain examples) so no field name becomes a rule.

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field, referenced as ``{{<base>}}``.
        target_field: The field this prompt generates.
        old_prompt: The current prompt to edit minimally.
        kept_deps: The dependencies (besides the change) that must remain referenced as-is.
        change: The single add/remove/toggle edit to apply.

    Returns:
        A single user message instructing the one-edit rewrite (output is the prompt only).
    """
    instruction = _edge_change_instruction(change)
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}. '
        f'Field to generate: "{target_field}".\n\n'
        "Current prompt:\n"
        f'"""\n{old_prompt.strip()}\n"""\n\n'
        "Apply EXACTLY this one dependency change and nothing else, staying as close to the "
        f"current wording as possible:\n{instruction}\n\n"
        f"Keep every OTHER dependency exactly as it is: {_deps_listing(kept_deps)}.\n\n"
        "General intuitions (apply the SHAPE, do not treat any field name as a rule): a HARD "
        "reference is one the output fundamentally is-about — the prompt must genuinely require "
        "that field; a SOFT reference is optional metadata that only sharpens the output — use "
        "it when present and self-guard (produce a valid result anyway) when it is empty. For "
        "example, an output can be produced from a single source field alone, but a richer "
        "related field can be woven in as optional context that makes it more precise.\n\n"
        "Output ONLY the rewritten prompt text — no commentary, no quotes, no code fences."
    )


def _edge_change_instruction(change: EdgeChange) -> str:
    """The one-line, generic instruction for a single add/remove/toggle edge change."""
    ref = f"{{{{{change.src}}}}}"
    if change.action == "add":
        if change.new_kind == "soft":
            return (
                f"Add a SOFT reference to {ref}: weave it in as optional metadata that sharpens "
                "the output, and self-guard so the field still produces a valid result when "
                f"{ref} is empty."
            )
        return (
            f"Add a HARD reference to {ref}: the field now genuinely requires it — make the "
            "output fundamentally about / grounded in that field."
        )
    if change.action == "remove":
        return (
            f"Stop depending on {ref}: remove its reference entirely from the prompt."
        )
    # toggle
    if change.new_kind == "soft":
        return (
            f"Make the existing reference to {ref} SOFT: keep using it when present, but "
            "self-guard so the field still produces a valid result when it is empty."
        )
    return (
        f"Make the existing reference to {ref} HARD: the field now genuinely requires it "
        "rather than using it as optional context."
    )


def build_improve_in_popover_message(
    note_type: str,
    base_field: str,
    target_field: str,
    prompt: str,
    fixed_deps: list[FieldDep],
) -> str:
    """Build the message improving a prompt's wording while PINNING its dependency set.

    The model may polish phrasing/guards/output rules but must reference EXACTLY the fields in
    ``fixed_deps`` — it may not add a new ``{{ref}}`` or drop an existing one.

    Args:
        note_type: The note type's name (context for the model).
        base_field: The always-present input field, referenced as ``{{<base>}}``.
        target_field: The field this prompt generates.
        prompt: The current prompt to polish.
        fixed_deps: The exact dependency set the rewrite must keep (no additions, no removals).

    Returns:
        A single user message instructing the pinned improvement (output is the prompt only).
    """
    return (
        f'Note type: "{note_type}". Base (input) field: {{{{{base_field}}}}}. '
        f'Field to generate: "{target_field}".\n\n'
        "Current prompt:\n"
        f'"""\n{prompt.strip()}\n"""\n\n'
        "Improve ONLY the wording — clarity, self-guards around empty fields, and Anki-friendly "
        "output rules. You MUST reference EXACTLY these fields and no others (do not add a new "
        f"field reference, do not drop one): {_deps_listing(fixed_deps)}.\n\n"
        "Output ONLY the improved prompt text — no commentary, no quotes, no code fences."
    )


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

    # -- two-way sync ------------------------------------------------------

    def classify_dependencies(
        self,
        *,
        note_type: str,
        base_field: str,
        target_field: str,
        prompt: str,
        refs: list[str],
    ) -> tuple[EdgeKinding, ...]:
        """Label each of ``refs`` as a hard or soft dependency of ``target_field``.

        Returns ``()`` immediately — WITHOUT calling the LLM — when ``refs`` is empty (nothing
        to classify). Otherwise builds the classifier message, calls the model at the
        deterministic classify temperature, and parses the reply.

        Raises:
            ProviderError: On a provider failure or an unparseable reply.
        """
        if not refs:
            return ()
        message = build_classify_deps_message(
            note_type, base_field, target_field, prompt, refs
        )
        raw = self._llm.generate_text(
            message,
            system=DEPENDENCY_CLASSIFIER_SYSTEM,
            temperature=envs.OMNIA_SMART_NOTES_CLASSIFY_DEPS_TEMPERATURE,
        )
        return parse_classified_deps(raw)

    def classify_dependencies_batch(
        self,
        *,
        note_type: str,
        base_field: str,
        items: list[tuple[str, str, list[str]]],
    ) -> dict[str, tuple[EdgeKinding, ...]]:
        """Classify many fields' refs hard/soft in ONE LLM call (cost mitigation).

        The batched counterpart of :meth:`classify_dependencies`: a big note type (dozens of
        fields) would otherwise fan out to one LLM call per field on Auto-prompt / Improve-all.
        Returns ``{}`` immediately — WITHOUT calling the LLM — when EVERY item has empty refs
        (nothing to classify). Items whose refs are empty are dropped from the request (the model
        is only asked about fields that actually reference something); the caller reconciles those
        empty-ref fields itself (to drop a vanished derived edge), no model needed. Uses the
        deterministic classify temperature.

        Args:
            note_type: The note type's name (context for the model).
            base_field: The always-present input field, referenced as ``{{<base>}}``.
            items: ``(target_field, prompt, refs)`` triples. ``refs`` should be the field names
                needing classification (typically the NEW refs); passing all refs is harmless —
                the reconciler keeps existing kinds regardless.

        Returns:
            A mapping of target field name → its classified ``EdgeKinding`` tuple. Fields whose
            refs were empty are absent.

        Raises:
            ProviderError: On a provider failure or an unparseable reply.
        """
        pending = [
            (target_field, prompt, refs) for target_field, prompt, refs in items if refs
        ]
        if not pending:
            return {}
        message = build_classify_deps_batch_message(note_type, base_field, pending)
        raw = self._llm.generate_text(
            message,
            system=DEPENDENCY_CLASSIFIER_SYSTEM,
            temperature=envs.OMNIA_SMART_NOTES_CLASSIFY_DEPS_TEMPERATURE,
        )
        return parse_classified_deps_batch(raw)

    def rewrite_for_edge_change(
        self,
        *,
        note_type: str,
        base_field: str,
        target_field: str,
        old_prompt: str,
        kept_deps: list[FieldDep],
        change: EdgeChange,
        known_fields: list[str],
        intended_depends_on: list[FieldDep],
    ) -> PromptRewrite:
        """Rewrite a prompt to reflect ONE graph edge change, guarded at the Python boundary.

        Asks the model to apply ``change`` (keeping ``kept_deps``), then VERIFIES the rewrite
        derives the INTENDED dependency edge set before accepting it. The intended set is derived
        from ``intended_depends_on`` (which already carries the change's kind — e.g. a soft add
        carries the soft kind), so a soft edge is validated against soft and does not falsely
        fail just because a bare derived ref defaults to hard. On a mismatch / bad syntax it does
        ONE bounded repair retry; if still inconsistent it returns the unchanged ``old_prompt``
        with ``ok=False``.

        Raises:
            ProviderError: On a provider/network failure.
        """
        message = build_edge_change_message(
            note_type, base_field, target_field, old_prompt, kept_deps, change
        )
        return self._guarded_rewrite(
            message=message,
            old_prompt=old_prompt,
            target_field=target_field,
            intended_depends_on=intended_depends_on,
            known_fields=known_fields,
            temperature=envs.OMNIA_SMART_NOTES_REWRITE_EDGE_TEMPERATURE,
        )

    def improve_in_popover(
        self,
        *,
        note_type: str,
        base_field: str,
        target_field: str,
        prompt: str,
        fixed_deps: list[FieldDep],
        known_fields: list[str],
    ) -> PromptRewrite:
        """Improve a prompt's wording while PINNING its dependency set, guarded at the boundary.

        Same consistency gate as :meth:`rewrite_for_edge_change`, but the intended edge set is
        ``fixed_deps`` itself: a rewrite that adds or drops a field reference fails the gate and
        the unchanged ``prompt`` is returned with ``ok=False``. Reuses the existing improve
        temperature (``OMNIA_SMART_NOTES_IMPROVE_PROMPT_TEMPERATURE``) — the in-popover improve
        is the same single-prompt polish task, just with a pinned dependency set.

        Raises:
            ProviderError: On a provider/network failure.
        """
        message = build_improve_in_popover_message(
            note_type, base_field, target_field, prompt, fixed_deps
        )
        return self._guarded_rewrite(
            message=message,
            old_prompt=prompt,
            target_field=target_field,
            intended_depends_on=fixed_deps,
            known_fields=known_fields,
            temperature=envs.OMNIA_SMART_NOTES_IMPROVE_PROMPT_TEMPERATURE,
        )

    def _guarded_rewrite(
        self,
        *,
        message: str,
        old_prompt: str,
        target_field: str,
        intended_depends_on: list[FieldDep],
        known_fields: list[str],
        temperature: float,
    ) -> PromptRewrite:
        """Run a rewrite request through the consistency gate with one bounded repair retry.

        The INTENDED edge set is derived ONCE from a synthetic prompt that references every field
        in ``intended_depends_on`` (so the full intended REF set is captured) UNIONed with
        ``intended_depends_on`` itself (so each edge carries its intended kind — a soft add is
        validated against soft). Each candidate prompt is then derived FROM ITS OWN ``{{refs}}``
        ONLY (no explicit deps) and diffed against the intended set: the gate passes when no ref
        was added or removed and the candidate's braces are well-formed. A KIND difference does
        NOT fail the gate (:class:`~omnia.plugins.smart_notes.engine.consistency.ConsistencyResult`
        leaves ``ok`` True on a kind change), which is exactly why a soft add — whose prompt ref
        derives a default-hard edge — does not falsely fail. A first failure re-asks the model
        once with the violation appended; a second failure returns ``old_prompt`` with
        ``ok=False``.
        """
        intended = NodeEdgeSet.derive(
            target_field,
            _synthetic_prompt(intended_depends_on),
            intended_depends_on,
            known_fields,
        )
        retry_message = message
        last_messages: tuple[str, ...] = ()
        for attempt in range(2):
            candidate = self._llm.generate_text(
                retry_message,
                system=FLASHCARD_EXPERT_SYSTEM,
                temperature=temperature,
            ).strip()
            after = NodeEdgeSet.derive(target_field, candidate, [], known_fields)
            result = intended.diff(after)
            if result.ok:
                return PromptRewrite(prompt=candidate, ok=True)
            last_messages = result.messages
            if attempt == 0:
                retry_message = (
                    f"{message}\n\nYour previous rewrite was rejected because it changed the "
                    "dependency set. Fix EXACTLY these issues and reference precisely the "
                    f"intended fields: {'; '.join(result.messages) or 'inconsistent references'}."
                )
        return PromptRewrite(
            prompt=old_prompt,
            ok=False,
            old_prompt=old_prompt,
            reason="; ".join(last_messages)
            or "rewrite did not match the intended dependencies",
        )
