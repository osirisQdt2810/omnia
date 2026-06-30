"""Pure rule compilation, selection, and prompt derivation for smart-notes generation.

No Anki imports. The data model is note-type-centric: a :class:`SmartNotesNoteTypeConfig`
names one base (input) field and configures how every OTHER field is generated. This module
compiles a note type's enabled, generatable fields into self-contained
:class:`~omnia.plugins.smart_notes.config.SmartNotesFieldRule` units
(:func:`compile_note_type_rules`), decides which rules to skip for a given note
(:func:`should_skip_rule`), derives the prompt / spoken text a rule needs
(:func:`prompt_for`, :func:`tts_text`), and exposes the batch-planning helpers
(:func:`dedupe_preserving_order`, :func:`chunk`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnia.plugins.smart_notes.engine.interpolation import (
    extract_field_refs,
    interpolate,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldConfig,
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )


def rule_source_fields(rule: SmartNotesFieldRule) -> list[str]:
    """Return the field names a rule DEPENDS on (prompt refs, or a real source field).

    These are the DERIVED dependencies the graph, ordering, and blocking read. They are the
    prompt's ``{{refs}}`` when a prompt is given; otherwise the rule's ``source_field`` — UNLESS
    that source is purely the empty-prompt → base fallback (``source_is_base_fallback``), in
    which case it is NOT a dependency and nothing is returned (so an empty-prompt field has no
    derived incoming edge). Generation reads ``source_field`` separately (see
    :func:`prompt_for` / :func:`tts_text`) and is unaffected.
    """
    if rule.prompt:
        return extract_field_refs(rule.prompt)
    if rule.source_field and not rule.source_is_base_fallback:
        return [rule.source_field]
    return []


def rule_prerequisites(rule: SmartNotesFieldRule) -> list[tuple[str, str]]:
    """Return ``(prerequisite_field, effective_kind)`` pairs for ``rule``.

    The SINGLE source of truth for what a rule depends on. Derived prerequisites (the prompt
    ``{{refs}}`` / ``source_field`` from :func:`rule_source_fields`) default to kind ``"hard"``;
    they are UNIONed with the rule's explicit ``depends_on`` entries, and an explicit entry for
    the same field OVERRIDES the derived kind (e.g. recolours a derived hard edge to soft).
    Field names keep their original case; de-duplication is case-insensitive (first occurrence
    wins for the display name). Ordering uses every pair (both kinds order); blocking filters to
    ``"hard"``; the graph builder adds its ``derived`` flag on top.

    Args:
        rule: The compiled generation rule.

    Returns:
        ``(field, kind)`` pairs in stable order: derived prerequisites first (in source order),
        then any explicit-only prerequisites, each with its effective kind.
    """
    override = {dep.field.strip().lower(): dep.kind for dep in rule.depends_on}
    prerequisites: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in rule_source_fields(rule):
        lower = name.strip().lower()
        if not lower or lower in seen:
            continue
        seen.add(lower)
        prerequisites.append((name.strip(), override.get(lower, "hard")))
    for dep in rule.depends_on:
        lower = dep.field.strip().lower()
        if not lower or lower in seen:
            continue
        seen.add(lower)
        prerequisites.append((dep.field.strip(), dep.kind))
    return prerequisites


def should_skip_rule(
    rule: SmartNotesFieldRule,
    fields: dict[str, str],
    *,
    allow_empty_fields: bool,
) -> bool:
    """Return whether ``rule`` should be skipped for a note with ``fields``.

    Two skip conditions:

    * **empty sources** — skip when the rule references fields but they are ALL blank,
      unless ``allow_empty_fields``. (A rule that references no field is never skipped on
      this account.)
    * **already filled** — skip when ``target_field`` already holds a value, unless the
      rule's own ``overwrite`` flag is set.

    Args:
        rule: The compiled generation rule under consideration.
        fields: The note's current field values (including any freshly chained values).
        allow_empty_fields: Generate even when all referenced source fields are blank.

    Returns:
        ``True`` if the rule must be skipped, ``False`` to generate it.
    """
    if not rule.overwrite and str(fields.get(rule.target_field, "")).strip():
        return True
    sources = rule_source_fields(rule)
    if sources and not allow_empty_fields:
        return not any(str(fields.get(name, "")).strip() for name in sources)
    return False


def compile_field_rule(
    field_config: SmartNotesFieldConfig, base_field: str
) -> SmartNotesFieldRule:
    """Compile ONE field config into a self-contained generation rule.

    The single site that turns a :class:`~omnia.plugins.smart_notes.config.SmartNotesFieldConfig`
    into a :class:`~omnia.plugins.smart_notes.config.SmartNotesFieldRule`, so generation, the
    dependency graph, and the consistency checker all derive a field's rule the same way and can
    never diverge. ``field`` → ``target_field``, ``type`` → ``kind``, the ``prompt`` template is
    carried through, and (for a field with no prompt) ``base_field`` is used as the implicit
    ``source_field`` so the field still generates from the base — that fallback source is flagged
    ``source_is_base_fallback`` so it is NOT treated as a derived dependency.

    Args:
        field_config: The persisted per-field config row.
        base_field: The note type's base (input) field, used as the empty-prompt source.

    Returns:
        The compiled :class:`~omnia.plugins.smart_notes.config.SmartNotesFieldRule`.
    """
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule

    has_prompt = bool(field_config.prompt)
    return SmartNotesFieldRule(
        source_field="" if has_prompt else base_field,
        source_is_base_fallback=not has_prompt,
        target_field=field_config.field,
        kind=field_config.type,
        prompt=field_config.prompt,
        provider=field_config.provider,
        model=field_config.model,
        voice=field_config.voice,
        language=field_config.language,
        overwrite=field_config.overwrite,
        depends_on=list(field_config.depends_on),
    )


def compile_note_type_rules(
    config: SmartNotesNoteTypeConfig,
) -> list[SmartNotesFieldRule]:
    """Compile a note type's enabled, generatable fields into self-contained rules.

    The base field is never compiled (it is the input). Each
    :class:`~omnia.plugins.smart_notes.config.SmartNotesFieldConfig` becomes one
    :class:`~omnia.plugins.smart_notes.config.SmartNotesFieldRule` the engine can run:

    * ``field`` → ``target_field`` and ``type`` → ``kind``.
    * The ``prompt`` template is carried through; for a text/image field with no prompt the
      base field is used as the implicit prompt source (``source_field``), so a bare field
      still generates from the base.
    * For a tts field the prompt is the spoken-text template (it may reference ``{{Base}}``);
      with no prompt the base field is spoken directly via ``source_field``.

    Args:
        config: The note type's smart-notes config.

    Returns:
        One rule per generatable field, in their configured order.
    """
    base = config.base_field
    return [
        compile_field_rule(field, base).copy(update={"note_type": config.note_type})
        for field in config.generatable_fields()
    ]


def applies_to_deck(config: SmartNotesNoteTypeConfig, deck_id: int) -> bool:
    """Whether this note-type config applies to a card in ``deck_id`` ([] decks = all decks)."""
    return not config.decks or int(deck_id) in config.decks


def dedupe_preserving_order(ids: list[int]) -> list[int]:
    """Return ``ids`` with duplicates removed, keeping first-seen order.

    A batch over a deck/note-type or a multi-card selection can list the same NOTE twice (two
    cards of one note); generation is per note, so the batch runner de-dupes note ids first.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def chunk(items: list[int], size: int) -> list[list[int]]:
    """Split ``items`` into consecutive batches of at most ``size`` (``size`` >= 1).

    The batch runner generates in chunks so it can update progress / honour a cancel between
    chunks instead of hammering the provider with the whole selection at once.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [items[start : start + size] for start in range(0, len(items), size)]


def prompt_for(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
    """The prompt for a text/image rule: the template if given, else the source field."""
    if rule.prompt:
        return interpolate(rule.prompt, fields)
    return fields.get(rule.source_field, "")


def tts_text(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
    """The text a tts rule speaks: the interpolated prompt, else the source field value."""
    if rule.prompt:
        return interpolate(rule.prompt, fields)
    return interpolate(fields.get(rule.source_field, ""), fields)
