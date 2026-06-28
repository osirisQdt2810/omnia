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
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )


def rule_source_fields(rule: SmartNotesFieldRule) -> list[str]:
    """Return the field names a rule reads (prompt refs, or its source_field)."""
    if rule.kind == "tts":
        if rule.prompt:
            return extract_field_refs(rule.prompt)
        return [rule.source_field] if rule.source_field else []
    if rule.prompt:
        return extract_field_refs(rule.prompt)
    return [rule.source_field] if rule.source_field else []


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
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule

    base = config.base_field
    rules: list[SmartNotesFieldRule] = []
    for field in config.generatable_fields():
        rules.append(
            SmartNotesFieldRule(
                note_type=config.note_type,
                source_field="" if field.prompt else base,
                target_field=field.field,
                kind=field.type,
                prompt=field.prompt,
                provider=field.provider,
                model=field.model,
                voice=field.voice,
                language=field.language,
                overwrite=field.overwrite,
            )
        )
    return rules


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
