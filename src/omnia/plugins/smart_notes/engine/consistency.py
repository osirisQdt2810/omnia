"""The shared prompt↔graph consistency seam for smart-notes.

Pure logic — no Anki imports. Two upcoming features change a field's dependencies from
opposite directions: prompt→graph (the user edits a prompt, we reclassify its edges) and
graph→prompt (the user edits the graph, we rewrite the prompt's ``{{refs}}``). BOTH validate
the change through THIS module, so the two directions can never drift: a field's incoming
dependency edges are derived ONCE here, reusing the same single source of truth the engine
and graph already use (:func:`~omnia.plugins.smart_notes.engine.rules.compile_field_rule` →
:func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`) plus the one syntax
authority (:func:`~omnia.plugins.smart_notes.engine.interpolation.validate_brace_syntax`).

A :class:`NodeEdgeSet` is the set of incoming ``(src_lower, kind)`` edges at one target field
(plus any syntax errors found in the prompt it was built from); :meth:`NodeEdgeSet.derive`
builds it from a candidate prompt and :meth:`NodeEdgeSet.diff` compares two of them into a
:class:`ConsistencyResult` with pre-rendered user copy naming the changed fields (e.g.
``Added dependency on "word"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnia.plugins.smart_notes.engine.interpolation import validate_brace_syntax
from omnia.plugins.smart_notes.engine.rules import (
    compile_field_rule,
    rule_prerequisites,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import FieldDep


def validate_prompt_syntax(prompt: str) -> list[str]:
    """Return human-readable brace-syntax errors for ``prompt`` (empty list = valid).

    Delegates to the single syntax authority
    (:func:`~omnia.plugins.smart_notes.engine.interpolation.validate_brace_syntax`) so the
    consistency seam never grows its own brace parser.
    """
    return validate_brace_syntax(prompt)


@dataclass(frozen=True)
class ConsistencyResult:
    """The diff between a ``before`` and ``after`` :class:`NodeEdgeSet`.

    ``added_fields`` / ``removed_fields`` are the source fields (display-lowercased) whose
    presence changed; ``kind_changes`` are ``(src_lower, before_kind, after_kind)`` for sources
    common to both whose kind flipped (e.g. ``hard`` → ``soft``). ``bad_syntax`` is True when the
    ``after`` prompt had brace-syntax errors. ``messages`` is ready-to-show English copy naming
    the fields. ``ok`` is True only when nothing was added, nothing removed, and the syntax is
    valid — a KIND CHANGE alone does NOT set ``ok`` False (it is reported for the caller to act
    on; the caller decides whether a recolour is acceptable).
    """

    ok: bool
    added_fields: tuple[str, ...]
    removed_fields: tuple[str, ...]
    kind_changes: tuple[tuple[str, str, str], ...]
    bad_syntax: bool
    messages: tuple[str, ...]


@dataclass(frozen=True)
class NodeEdgeSet:
    """The incoming dependency edges at one field, plus its prompt's syntax verdict.

    ``edges`` is the set of ``(src_lower, kind)`` pairs that point AT ``target`` — the field's
    derived prompt ``{{refs}}`` (default ``"hard"``) UNIONed with its explicit ``depends_on``,
    with edges to unknown fields and self-references dropped (an empty prompt ⇒ no edges, per
    the B3 rule). ``syntax_errors`` carries the messages
    :func:`~omnia.plugins.smart_notes.engine.interpolation.validate_brace_syntax` found in the
    prompt this set was built from (empty when the prompt's braces are well-formed), so
    :meth:`diff` can flag a malformed ``after`` prompt without re-reading it.
    """

    target: str
    edges: frozenset[tuple[str, str]]
    syntax_errors: tuple[str, ...] = ()

    @classmethod
    def derive(
        cls,
        target_field: str,
        prompt: str,
        depends_on: list[FieldDep],
        known_fields: list[str],
    ) -> NodeEdgeSet:
        """Derive the incoming dependency edges a candidate ``prompt`` implies at ``target_field``.

        Builds the same rule the engine and graph compile
        (:func:`~omnia.plugins.smart_notes.engine.rules.compile_field_rule`) and reads its
        prerequisites through the single source of truth
        (:func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`). Edges whose source
        is not in ``known_fields`` (case-insensitive) are dropped, as is a self-reference; an
        empty prompt yields no edges (the B3 rule, since the base fallback is not a derived
        dependency). The prompt's brace-syntax errors are carried on ``syntax_errors``.

        Args:
            target_field: The dependent field the edges point at.
            prompt: The candidate prompt template (its ``{{refs}}`` are derived hard edges).
            depends_on: The field's explicit dependency edges (union with derived; kind
                overrides).
            known_fields: The note type's field names; edges to anything else are dropped.

        Returns:
            A :class:`NodeEdgeSet` of incoming ``(src_lower, kind)`` edges plus the prompt's
            syntax errors.
        """
        from omnia.plugins.smart_notes.config import SmartNotesFieldConfig

        field_config = SmartNotesFieldConfig(
            field=target_field,
            enabled=True,
            prompt=prompt,
            depends_on=list(depends_on),
        )
        # base_field "" — the empty-prompt fallback is never a derived dependency (B3), so the
        # base cannot contribute an edge here regardless of what we pass.
        rule = compile_field_rule(field_config, "")
        known = {name.strip().lower() for name in known_fields if name.strip()}
        target_lower = target_field.strip().lower()
        edges: set[tuple[str, str]] = set()
        for src, kind in rule_prerequisites(rule):
            src_lower = src.strip().lower()
            if src_lower == target_lower:
                continue  # self-reference is not an edge
            if src_lower not in known:
                continue  # source is not a field of this note type
            edges.add((src_lower, kind))
        return cls(
            target=target_field,
            edges=frozenset(edges),
            syntax_errors=tuple(validate_prompt_syntax(prompt)),
        )

    def diff(self, after: NodeEdgeSet) -> ConsistencyResult:
        """Diff this (``before``) edge set against ``after`` into a consistency verdict.

        Compares by the SET OF SOURCE FIELDS first — sources only in ``after`` are
        ``added_fields``, sources only in ``self`` are ``removed_fields`` — then, for sources
        common to both, reports any kind flip in ``kind_changes``
        (``(src_lower, before_kind, after_kind)``). ``ok`` is True only when nothing was added,
        nothing removed, and ``after`` has valid brace syntax; a kind change alone does NOT set
        ``ok`` False (it is surfaced for the caller to decide on). ``messages`` is pre-rendered
        English copy naming each changed field (and any syntax error).

        Args:
            after: The field's edge set after the change.

        Returns:
            The :class:`ConsistencyResult` describing the change from ``self`` to ``after``.
        """
        before_kinds = dict(self.edges)
        after_kinds = dict(after.edges)
        before_srcs = set(before_kinds)
        after_srcs = set(after_kinds)

        added = sorted(after_srcs - before_srcs)
        removed = sorted(before_srcs - after_srcs)
        kind_changes = tuple(
            (src, before_kinds[src], after_kinds[src])
            for src in sorted(before_srcs & after_srcs)
            if before_kinds[src] != after_kinds[src]
        )
        bad_syntax = bool(after.syntax_errors)

        # Field names in the copy are the case-folded source names the edge sets carry (a
        # NodeEdgeSet stores src_lower, so the original display case is not available here).
        messages: list[str] = []
        messages.extend(f'Added dependency on "{src}"' for src in added)
        messages.extend(f'Removed dependency on "{src}"' for src in removed)
        messages.extend(
            f'Changed "{src}" dependency from {old} to {new}'
            for src, old, new in kind_changes
        )
        messages.extend(after.syntax_errors)

        ok = not added and not removed and not bad_syntax
        return ConsistencyResult(
            ok=ok,
            added_fields=tuple(added),
            removed_fields=tuple(removed),
            kind_changes=kind_changes,
            bad_syntax=bad_syntax,
            messages=tuple(messages),
        )
