"""Pure smart-notes logic: prompt interpolation + the provider-backed generation service.

No Anki imports. The data model is note-type-centric: a :class:`SmartNotesNoteTypeConfig`
names one base (input) field and configures how every OTHER field is generated. The engine
compiles a note type's enabled, generatable fields into self-contained
:class:`~omnia.core.config.models.SmartNotesFieldRule` units, orders them via the DAG
(``dag.py``), and runs each through the injected
:class:`~omnia.core.providers.ProviderHub` (DIP, so it's tested with a fake hub).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from omnia.core.providers.errors import ProviderError
from omnia.features.smart_notes.dag import order_rules
from omnia.features.smart_notes.markdown import convert_markdown_to_html

if TYPE_CHECKING:
    from omnia.core.config.models import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )
    from omnia.core.providers import ProviderHub

# {{FieldName}} placeholders, but NOT Anki cloze deletions ({{c1::...}}).
_FIELD_RE = re.compile(r"\{\{(?!c\d+::)([^{}]+?)\}\}")


def extract_field_refs(prompt: str) -> list[str]:
    """Return the field names referenced as ``{{Field}}`` in ``prompt``."""
    return [match.group(1).strip() for match in _FIELD_RE.finditer(prompt)]


def interpolate(prompt: str, fields: dict[str, str]) -> str:
    """Substitute ``{{Field}}`` placeholders in ``prompt`` with values from ``fields``."""
    return _FIELD_RE.sub(lambda m: str(fields.get(m.group(1).strip(), "")), prompt)


def _rule_source_fields(rule: SmartNotesFieldRule) -> list[str]:
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
    sources = _rule_source_fields(rule)
    if sources and not allow_empty_fields:
        return not any(str(fields.get(name, "")).strip() for name in sources)
    return False


def compile_note_type_rules(
    config: SmartNotesNoteTypeConfig,
) -> list[SmartNotesFieldRule]:
    """Compile a note type's enabled, generatable fields into self-contained rules.

    The base field is never compiled (it is the input). Each
    :class:`~omnia.core.config.models.SmartNotesFieldConfig` becomes one
    :class:`~omnia.core.config.models.SmartNotesFieldRule` the engine can run:

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
    from omnia.core.config.models import SmartNotesFieldRule

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
                overwrite=field.overwrite,
            )
        )
    return rules


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

    :meth:`generate` runs one rule; :meth:`generate_note` compiles a
    :class:`~omnia.core.config.models.SmartNotesNoteTypeConfig` into rules and runs them in
    dependency order, chaining each text result into the field map so a downstream rule sees
    the freshly generated value. Per-rule ``model``/``voice`` overrides layer on top of the
    central provider config.
    """

    def __init__(self, providers: ProviderHub) -> None:
        self._providers = providers

    def generate(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> GenerationResult:
        """Produce the content for ``rule`` from a note's ``fields``.

        A per-rule ``provider``/``model`` selects a provider INSTANCE configured with that
        model (the model is fixed at construction, never threaded per call); for TTS the
        spoken text is the interpolated prompt (or the interpolated source field when no
        prompt is given) and ``voice`` overrides the configured voice. Text results are
        rendered from Markdown to HTML for display in the card.

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
            data = provider.synthesize(
                self._tts_text(rule, fields), voice=rule.voice or None
            )
            return GenerationResult("tts", data=data, ext=provider.audio_ext)
        raise ProviderError(f"Unknown generation kind: {rule.kind!r}")

    def generate_note(
        self,
        config: SmartNotesNoteTypeConfig,
        fields: dict[str, str],
        *,
        allow_empty_fields: bool = False,
        force_overwrite: bool = False,
    ) -> list[tuple[SmartNotesFieldRule, GenerationResult]]:
        """Generate a note type's enabled fields, in dependency order, with chaining.

        The note type's generatable fields are compiled into rules
        (:func:`compile_note_type_rules`), topologically ordered
        (:func:`~omnia.features.smart_notes.dag.order_rules`) so a field that references
        another generated field runs after it, and each text result is written back into a
        working copy of ``fields`` so the dependent field interpolates the freshly generated
        value. The base field is never generated. Fields whose sources are all blank, or
        whose target is already filled, are skipped per :func:`should_skip_rule`.

        Args:
            config: The note type's smart-notes config (its base field + per-field rows).
            fields: The note's current field values (not mutated).
            allow_empty_fields: Generate even when all referenced source fields are blank.
            force_overwrite: Regenerate every field even if its target is already non-empty
                (the batch "regenerate when batching" path), ignoring per-field ``overwrite``.

        Returns:
            ``(rule, result)`` pairs for the fields that actually generated, in run order.

        Raises:
            SmartNotesCycleError: If the fields reference each other in a cycle.
            ProviderError: On bad config or a provider/network failure.
        """
        rules = compile_note_type_rules(config)
        if force_overwrite:
            rules = [rule.model_copy(update={"overwrite": True}) for rule in rules]
        working = dict(fields)
        results: list[tuple[SmartNotesFieldRule, GenerationResult]] = []
        for rule in order_rules(rules):
            if should_skip_rule(rule, working, allow_empty_fields=allow_empty_fields):
                continue
            result = self.generate(rule, working)
            results.append((rule, result))
            # Only text feeds downstream prompts; media (image/tts) becomes an embed ref a
            # later prompt shouldn't consume.
            if result.kind == "text" and result.text is not None:
                working[rule.target_field] = result.text
        return results

    @staticmethod
    def _prompt(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
        """The prompt for a text/image rule: the template if given, else the source field."""
        if rule.prompt:
            return interpolate(rule.prompt, fields)
        return fields.get(rule.source_field, "")

    @staticmethod
    def _tts_text(rule: SmartNotesFieldRule, fields: dict[str, str]) -> str:
        """The text a tts rule speaks: the interpolated prompt, else the source field value."""
        if rule.prompt:
            return interpolate(rule.prompt, fields)
        return interpolate(fields.get(rule.source_field, ""), fields)
