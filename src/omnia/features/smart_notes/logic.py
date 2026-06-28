"""Pure smart-notes logic: prompt interpolation + the provider-backed generation service.

No Anki imports. The :class:`GenerationService` depends on the injected
:class:`~omnia.core.providers.ProviderHub` (DIP), so it's tested with a fake hub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.providers.errors import ProviderError
from omnia.features.smart_notes.dag import order_rules
from omnia.features.smart_notes.markdown import convert_markdown_to_html

if TYPE_CHECKING:
    from omnia.core.config.models import SmartNotesFieldRule
    from omnia.core.providers import ProviderHub

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")

# The valid generation kinds; rows with anything else collapse to the default.
_VALID_KINDS = ("text", "image", "tts")
# The fields the dialog rows carry, in column order.
_ROW_KEYS = ("note_type", "source_field", "target_field", "kind", "prompt")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)


def _rule_source_fields(rule: SmartNotesFieldRule) -> list[str]:
    """Return the field names a rule reads (prompt refs, or its source_field)."""
    if rule.kind == "tts":
        return [rule.source_field] if rule.source_field else []
    if rule.prompt:
        return extract_field_refs(rule.prompt)
    return [rule.source_field] if rule.source_field else []


def should_skip_rule(
    rule: SmartNotesFieldRule,
    fields: dict[str, str],
    *,
    allow_empty_fields: bool,
    overwrite: bool,
) -> bool:
    """Return whether ``rule`` should be skipped for a note with ``fields``.

    Mirrors the reference add-on's two skip conditions:

    * **empty sources** — like ``prompt_helpers.interpolate_prompt`` returning ``None``: skip
      when the rule references fields but they are ALL blank, unless ``allow_empty_fields``.
      (A rule that references no field is never skipped on this account.)
    * **already filled** — skip when ``target_field`` already holds a value, unless
      ``overwrite`` is set.

    Args:
        rule: The generation rule under consideration.
        fields: The note's current field values (including any freshly chained values).
        allow_empty_fields: Generate even when all referenced source fields are blank.
        overwrite: Regenerate even when the target field is already non-empty.

    Returns:
        ``True`` if the rule must be skipped, ``False`` to generate it.
    """
    if not overwrite and str(fields.get(rule.target_field, "")).strip():
        return True
    sources = _rule_source_fields(rule)
    if sources and not allow_empty_fields:
        return not any(str(fields.get(name, "")).strip() for name in sources)
    return False


def rules_to_rows(rules: list[SmartNotesFieldRule]) -> list[dict[str, str]]:
    """Project field rules into plain dict rows for the field-mapping dialog table."""
    return [{key: getattr(rule, key) for key in _ROW_KEYS} for rule in rules]


def rows_to_rules(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalise dialog rows into rule dicts, dropping blank rows.

    Whitespace is stripped from each cell; a row is dropped if every cell is empty after
    stripping. An unrecognised ``kind`` falls back to ``"text"`` so a stray combo value can't
    fail validation; real validation happens when the dialog builds ``SmartNotesSettings``.
    """
    cleaned: list[dict[str, str]] = []
    for row in rows:
        values = {key: str(row.get(key, "") or "").strip() for key in _ROW_KEYS}
        if not any(values.values()):
            continue  # a fully blank row is the table's "add another" affordance
        if values["kind"] not in _VALID_KINDS:
            values["kind"] = "text"
        cleaned.append(values)
    return cleaned


def rule_matches_note_type(rule: SmartNotesFieldRule, note_type: str) -> bool:
    """Return whether ``rule`` applies to a note of ``note_type`` (empty matches any)."""
    return not rule.note_type or rule.note_type == note_type


def select_rules_for_note(
    rules: list[SmartNotesFieldRule],
    note_type: str,
    field_names: list[str],
    *,
    enabled_only: bool = False,
) -> list[SmartNotesFieldRule]:
    """Return the rules that apply to one note (its ``note_type`` + existing ``field_names``).

    A rule applies when its (optional) ``note_type`` matches and its ``target_field`` is one of
    the note's fields. ``enabled_only`` keeps only rules flagged for automatic batching — the
    Browser/sidebar batch passes it so a disabled rule is skipped there, while the editor button
    and the per-field context-menu action run disabled rules too (mirroring the reference, where
    a disabled field is still manually generatable).

    Args:
        rules: All configured field rules.
        note_type: The note's note-type name.
        field_names: The note's field names.
        enabled_only: Drop rules whose ``enabled`` flag is False.

    Returns:
        The matching rules, in their configured order.
    """
    present = set(field_names)
    return [
        rule
        for rule in rules
        if rule_matches_note_type(rule, note_type)
        and rule.target_field in present
        and (rule.enabled or not enabled_only)
    ]


def rules_for_field(
    rules: list[SmartNotesFieldRule], note_type: str, field: str
) -> list[SmartNotesFieldRule]:
    """Return the rules of ``note_type`` whose ``target_field`` is ``field`` (any enabled state).

    Used by the field context-menu "Generate this field" action, which runs a single field's
    rule(s) on demand even when they are disabled for automatic batching.
    """
    return [
        rule
        for rule in rules
        if rule_matches_note_type(rule, note_type) and rule.target_field == field
    ]


def build_generation_plan(
    fields: dict[str, str], note_type: str, rules: list[SmartNotesFieldRule]
) -> list[tuple[SmartNotesFieldRule, dict[str, str]]]:
    """Select the rules that apply to one note's ``fields`` of type ``note_type``.

    A rule applies when its (optional) ``note_type`` matches and its ``target_field`` exists
    on the note. Pure so both the Browser menu and the editor button share the same selection
    logic; each pairs the rule with the note's current field values for generation.
    """
    return [
        (rule, fields)
        for rule in rules
        if rule_matches_note_type(rule, note_type) and rule.target_field in fields
    ]


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


@dataclass
class GenerationResult:
    """The output of one generation rule."""

    kind: str  # text | image | tts
    text: Optional[str] = None
    data: Optional[bytes] = None
    ext: str = ""


class GenerationService:
    """Runs field-generation rules against the configured providers.

    :meth:`generate` runs one rule; :meth:`generate_note` runs all of a note's rules in
    dependency order, chaining each text result into the field map so a downstream rule sees
    the freshly generated value (mirroring the reference add-on's DAG processing). Per-rule
    ``model``/``voice`` overrides layer on top of the central provider config.
    """

    def __init__(self, providers: ProviderHub) -> None:
        self._providers = providers

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        A per-rule ``provider``/``model`` selects a provider INSTANCE configured with that
        model (the model is fixed at construction, never threaded per call); for TTS the
        source field is interpolated like a text prompt and ``voice`` overrides the configured
        voice. Text results are rendered from Markdown to HTML for display in the card.

        Raises:
            ProviderError: On bad config or a provider/network failure.
        """
        if rule.kind == "text":
            llm = self._providers.llm(model=rule.model, provider=rule.provider)
            text = llm.generate_text(self._prompt(rule, fields))
            return GenerationResult("text", text=convert_markdown_to_html(text))
        if rule.kind == "image":
            llm = self._providers.llm(model=rule.model, provider=rule.provider)
            data = llm.generate_image(self._prompt(rule, fields))
            return GenerationResult("image", data=data, ext="png")
        if rule.kind == "tts":
            provider = self._providers.tts()
            # Read the source field's value, then interpolate any {{Field}} refs it carries
            # (so a templated source resolves the same way a text prompt does).
            source = interpolate(fields.get(rule.source_field, ""), fields)
            data = provider.synthesize(source, voice=rule.voice or None)
            return GenerationResult("tts", data=data, ext=provider.audio_ext)
        raise ProviderError(f"Unknown generation kind: {rule.kind!r}")

    def generate_note(
        self,
        rules: list[SmartNotesFieldRule],
        fields: dict[str, str],
        *,
        allow_empty_fields: bool = False,
        overwrite: bool = False,
    ) -> list[tuple[SmartNotesFieldRule, GenerationResult]]:
        """Generate every applicable rule for one note, in dependency order, with chaining.

        Rules are topologically ordered (:func:`~omnia.features.smart_notes.dag.order_rules`)
        so a rule that references another rule's ``target_field`` runs after it; each text
        result is written back into a working copy of ``fields`` so the dependent rule
        interpolates the freshly generated value. Rules whose sources are all blank or whose
        target is already filled are skipped per :func:`should_skip_rule`.

        Args:
            rules: The rules selected for this note (e.g. via :func:`build_generation_plan`).
            fields: The note's current field values (not mutated).
            allow_empty_fields: Generate even when all referenced source fields are blank.
            overwrite: Regenerate even when the target field is already non-empty.

        Returns:
            ``(rule, result)`` pairs for the rules that actually generated, in run order.

        Raises:
            SmartNotesCycleError: If the rules form a cycle or a rule references itself.
            ProviderError: On bad config or a provider/network failure.
        """
        working = dict(fields)
        results: list[tuple[SmartNotesFieldRule, GenerationResult]] = []
        for rule in order_rules(rules):
            if should_skip_rule(
                rule,
                working,
                allow_empty_fields=allow_empty_fields,
                overwrite=overwrite,
            ):
                continue
            result = self.generate(rule, working)
            results.append((rule, result))
            # Only text feeds downstream prompts; media (image/tts) becomes an embed ref a
            # later prompt shouldn't consume, matching the reference's field-type rules.
            if result.kind == "text" and result.text is not None:
                working[rule.target_field] = result.text
        return results

    @staticmethod
    def _prompt(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
        """The prompt for a text/image rule: the template if given, else the source field."""
        if rule.prompt:
            return interpolate(rule.prompt, fields)
        return fields.get(rule.source_field, "")
