"""Rewriting a note type's Smart Notes config onto DIFFERENT field names.

This is the whole difficulty of moving a configuration between collections. A field name is
not written down once — it is written down in six places, and a rename that reaches five of
them produces a config that still loads, still renders, and is quietly wrong:

======================  ==========================================================
where                   what a missed rename does
======================  ==========================================================
``fields[].field``      the rule targets a field that no longer exists → dead rule
``base_field``          every rule loses its source → nothing generates
``depends_on[].field``  a stale HARD edge blocks generation forever, with no reason
                        shown; a stale soft edge silently drops an ordering
``node_positions``      the dependency graph re-lays-out from scratch
``prompt`` ``{{refs}}`` the model is handed a placeholder that interpolates to ""
tool ``params``         a ``cloze``/``cloze_audio``/user tool reads the wrong field
======================  ==========================================================

The last one is the trap: the param KEY differs per tool (``sentence_field``, ``source_field``,
``word_field``, whatever a user tool invented), so there is no fixed list to rewrite. Rather
than hard-code one, this module asks each tool through
:meth:`~omnia.plugins.smart_notes.engine.tools.base.Tool.referenced_fields` — the SAME contract
the dependency graph uses to derive tool edges. A param that is a field reference to the graph
is a param this rewrites, by construction; a tool that declares nothing is REPORTED rather than
guessed at, because silently rewriting a value that merely looks like a field name is how you
corrupt a working chain.

Pure logic: no ``aqt``/``anki``, so it unit-tests headless.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from omnia.plugins.smart_notes.config import (
    FieldDep,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine.interpolation import (
    extract_field_refs,
    rename_field_refs,
)


@dataclass
class RemapReport:
    """What a remap did, and what it could not do.

    Every entry here is something the user has to know: a dropped rule is configuration they
    lose, and an undeclared tool param is a value this module deliberately did NOT touch.
    """

    renamed: dict[str, str] = dataclass_field(default_factory=dict)
    dropped_fields: list[str] = dataclass_field(default_factory=list)
    dropped_dependencies: list[str] = dataclass_field(default_factory=list)
    unresolved_prompt_refs: list[str] = dataclass_field(default_factory=list)
    unchecked_tool_params: list[str] = dataclass_field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.dropped_fields
            or self.dropped_dependencies
            or self.unresolved_prompt_refs
            or self.unchecked_tool_params
        )


def _tool_referenced(spec: FieldToolConfig) -> tuple[list[str], bool]:
    """Return ``(field names this tool's params name, the tool answered)``.

    ``answered`` is False when the tool is not installed here or never overrode
    ``referenced_fields`` — the difference between "no field params" and "I cannot tell you",
    which is what separates a clean remap from one the user must check by hand.
    """
    from omnia.plugins.smart_notes.engine.tools.base import Tool
    from omnia.plugins.smart_notes.engine.tools.registry import get_tool

    cls = get_tool(spec.tool)
    declared = getattr(cls, "referenced_fields", None)
    if cls is None or declared is None:
        return [], False
    # Compare the underlying FUNCTIONS. ``referenced_fields`` is a classmethod, so every
    # attribute access builds a fresh bound-method object and ``Sub.m is Base.m`` is False even
    # for a plain inheritance — which would report every tool as having declared, and the
    # "nobody checked this param" warning would never fire.
    declares = (
        getattr(declared, "__func__", declared) is not Tool.referenced_fields.__func__
    )
    try:
        names = [str(name) for name in declared(spec.params) if name]
    except Exception:
        # Same guard the graph path uses: params here have not been through parse_params.
        return [], False
    return names, declares


def _remap_tool(
    spec: FieldToolConfig,
    renames: Mapping[str, str],
    owner: str,
    report: RemapReport,
) -> FieldToolConfig:
    """Rewrite the field-naming params of one tool in a chain."""
    referenced, declares = _tool_referenced(spec)
    targets = {name for name in referenced if name in renames}
    params: dict[str, Any] = {}
    for key, value in spec.params.items():
        if isinstance(value, str) and value in targets:
            params[key] = renames[value]
            continue
        params[key] = value
        moved = isinstance(value, str) and renames.get(value, value) != value
        if moved and not declares:
            # The value IS a name this remap is moving, but the tool never claimed it as a
            # field reference. Rewriting on a guess breaks a chain that stored a literal;
            # leaving it silently hands over a tool reading a field that no longer exists.
            # Neither is ours to choose, so report it.
            report.unchecked_tool_params.append(
                f"{owner}: {spec.tool}.{key} = {value!r}"
            )
    return FieldToolConfig(tool=spec.tool, params=params)


def remap_note_type_config(
    config: SmartNotesNoteTypeConfig,
    renames: Mapping[str, str],
    *,
    note_type_name: str | None = None,
    keep_unmapped: bool = False,
) -> tuple[SmartNotesNoteTypeConfig, RemapReport]:
    """Return ``config`` rewritten onto the field names ``renames`` gives, plus a report.

    Args:
        config: The source note type's Smart Notes configuration.
        renames: ``{source field name: target field name}``. A source field absent from the
            mapping has no counterpart on the target note type.
        note_type_name: Rename the note type itself (``None`` keeps the source's name).
        keep_unmapped: Keep rules for fields with no target instead of dropping them. Off by
            default: a rule whose target field does not exist can never generate, and keeping
            it means the Fields table shows a row the user cannot act on.

    Returns:
        ``(remapped config, report)``. The report names every rule and edge that was dropped
        and every tool param this could not verify.
    """
    report = RemapReport(renamed=dict(renames))
    source_names = {rule.field for rule in config.fields}
    if config.base_field:
        source_names.add(config.base_field)
    known = frozenset(source_names)

    def target_of(name: str) -> str | None:
        if name in renames:
            return renames[name]
        return name if keep_unmapped else None

    kept: list[SmartNotesFieldConfig] = []
    for rule in config.fields:
        new_name = target_of(rule.field)
        if new_name is None:
            report.dropped_fields.append(rule.field)
            continue

        deps: list[FieldDep] = []
        for dep in rule.depends_on:
            dep_target = target_of(dep.field)
            if dep_target is None:
                report.dropped_dependencies.append(f"{rule.field} -> {dep.field}")
                continue
            deps.append(FieldDep(field=dep_target, kind=dep.kind, auto=dep.auto))

        prompt = rename_field_refs(rule.prompt, renames)
        for ref in extract_field_refs(prompt):
            # A ref that names neither a target field nor anything we knew about is either a
            # typo the user already had, or a field that did not survive the mapping.
            if ref in known and ref not in renames.values():
                report.unresolved_prompt_refs.append(f"{rule.field}: {{{{{ref}}}}}")

        tools = [_remap_tool(spec, renames, rule.field, report) for spec in rule.tools]

        kept.append(
            rule.copy(
                update={
                    "field": new_name,
                    "depends_on": deps,
                    "prompt": prompt,
                    "tools": tools,
                }
            )
        )

    base = config.base_field
    if base:
        mapped_base = target_of(base)
        base = mapped_base if mapped_base is not None else ""
        if not base:
            report.dropped_fields.append(config.base_field)

    positions = {}
    for name, position in config.node_positions.items():
        moved = target_of(name)
        if moved is not None:
            positions[moved] = list(position)

    remapped = config.copy(
        update={
            "note_type": note_type_name or config.note_type,
            "base_field": base,
            "fields": kept,
            "node_positions": positions,
        }
    )
    return remapped, report


def identity_renames(config: SmartNotesNoteTypeConfig) -> dict[str, str]:
    """The no-op mapping for ``config`` — every field name onto itself.

    The starting point a UI presents for an overwrite: the user only has to correct the rows
    where the two note types actually disagree.
    """
    names = [rule.field for rule in config.fields]
    if config.base_field:
        names.append(config.base_field)
    return {name: name for name in dict.fromkeys(names)}


def suggest_renames(
    source_fields: list[str], target_fields: list[str]
) -> dict[str, str]:
    """Pair source field names with target ones, exactly first then case-insensitively.

    Only the unambiguous pairings: anything else is left for the user, because a wrong guess
    here rewrites prompts and tool params onto the wrong field, which is far more expensive to
    notice than an unmapped row the UI asks about.
    """
    remaining = list(target_fields)
    mapping: dict[str, str] = {}
    for name in source_fields:
        if name in remaining:
            mapping[name] = name
            remaining.remove(name)
    for name in source_fields:
        if name in mapping:
            continue
        folded = [t for t in remaining if t.lower() == name.lower()]
        if len(folded) == 1:
            mapping[name] = folded[0]
            remaining.remove(folded[0])
    return mapping
