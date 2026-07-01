"""The two-way prompt↔graph dependency-sync controller (Features 1 & 2).

Feature 1 (prompt → graph): when a field's prompt changes, classify its NEW ``{{refs}}``
hard/soft off-thread and recolour the dependency graph. Feature 2 (graph → prompt): when a
graph edge changes, rewrite the affected node's prompt to match (guard-railed), validate a
candidate prompt against an intended edge set (synchronous, no LLM), and improve a prompt while
pinning its dependency set.

Holds the per-dialog ``_deps_memo`` so re-classifying an unchanged prompt reuses the cached
verdicts (correctness never depends on it — the reconcile re-derives the edge set every time).
Off-thread pushes happen ONLY from the ``run_in_background`` success/failure callbacks (main
thread); an off-main ``eval_js`` hard-crashes Qt. Only loaded inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.gui.smart_notes.html import graph_payload, note_type_config_from_payload

logger = get_logger("smart_notes")


@dataclass(frozen=True)
class _DepPlan:
    """What a ``classify_deps`` request must do per row: classify, reuse a memo, or just reconcile.

    ``uncached_items`` are the ``(field, prompt, refs)`` triples whose (all) refs need a fresh LLM
    classify (no memo hit) — the prompt is the source of truth for hard/soft, so EVERY ref is
    re-classified, not only newly-added ones; ``cached`` is ``{field: {ref: kind}}`` for rows whose
    identical prompt was already classified this session (the memo — an unchanged prompt costs no
    LLM call). Rows with no refs appear in neither — they still reconcile (to drop a vanished
    derived edge) with no verdicts.
    """

    uncached_items: list[tuple[str, str, list[str]]] = dataclass_field(
        default_factory=list
    )
    cached: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)


class GraphController:
    """Two-way prompt↔graph dependency sync (Features 1 & 2)."""

    def __init__(self, ctx: SmartNotesContext) -> None:
        self._ctx = ctx
        # Per-dialog memo for the prompt→graph dependency sync: re-saving an unchanged prompt
        # reuses the cached classifier verdicts so the LLM isn't re-called; correctness never
        # depends on it (the reconcile re-derives the edge set every time). Keyed by the FULL
        # classifier context (note_type, base_field, field, prompt) — every input that can change
        # the verdict — so one dialog instance serving many note types can never reuse a verdict
        # computed under a different note type / base field.
        self._deps_memo: dict[tuple[str, str, str, str], dict[str, str]] = {}

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "graph_recompute": self.on_graph_recompute,
            "classify_deps": self.on_classify_deps,
            "validate_prompt": self.on_validate_prompt,
            "rewrite_edges": self.on_rewrite_edges,
            "improve_prompt_pinned": self.on_improve_prompt_pinned,
        }

    def on_graph_recompute(self, data: dict[str, Any]) -> dict[str, Any]:
        """Re-lay out the field dependency graph from the page's current rows.

        The JS posts the live rows (including each row's ``depends_on``) after every structural
        edge edit and on first opening the Dependencies view; this builds a config and returns a
        freshly laid-out :func:`graph_payload` so the layout is always computed in Python. A
        cycle (the server-side backstop via ``FieldGraph.from_config`` / ``laid_out``) or any
        other failure returns ``{error}`` so the dialog never crashes and the page can revert the
        optimistic change.
        """
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            positions=dict(data.get("positions", {})),
        )
        try:
            return {"graph": graph_payload(config)}
        except (
            Exception
        ) as exc:  # boundary: a cycle/bad payload must not crash the dialog
            logger.exception("smart_notes: failed to recompute field graph")
            return {"error": f"Could not lay out the graph: {exc}"}

    # --- prompt→graph dependency sync (Feature 1) ------------------------------------
    def on_classify_deps(self, data: dict[str, Any]) -> None:
        """Classify a changed field's refs hard/soft off-thread, then recolour the graph.

        Feature 1 (prompt → graph): when a field's prompt changes the page posts that field (and
        its live ``depends_on``) here. The prompt is the SOURCE OF TRUTH for hard/soft, so ALL of
        its ``{{refs}}`` are (re)classified — :meth:`reconcile_field_deps` re-colours each existing
        edge to the fresh verdict (a prompt edit that flips required↔optional flips hard↔soft). The
        page's guard rail already blocked junk refs (fields that don't exist) before Save, so the
        classifier only ever sees real field edges. The single batched LLM call runs in ONE
        :func:`run_in_background`; the SUCCESS callback (main thread) reconciles each row and pushes
        the recoloured ``depends_on`` via ``window.__snDepsResult`` — the push MUST happen there,
        never inside the worker (off-thread ``eval_js`` hard-crashes Qt).

        A row whose prompt references nothing still reconciles (to DROP a now-vanished derived
        edge) — that path needs no LLM. A per-instance memo keyed by
        ``(note_type, base_field, field, prompt)`` avoids a repeat classify of an unchanged
        prompt; correctness never depends on it.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        rows = list(data.get("rows", []))
        plan = self._plan_dep_classification(note_type, base_field, rows)
        if plan.uncached_items:
            hub = self._ctx.build_hub()
            if hub is None:
                self._push_deps_result(error="Provider config error — see logs.")
                return

            from omnia.plugins.smart_notes.authoring import PromptAuthor

            anki_compat.run_in_background(
                lambda: PromptAuthor(hub.llm()).classify_dependencies_batch(
                    note_type=note_type,
                    base_field=base_field,
                    items=plan.uncached_items,
                ),
                on_success=lambda classified: self._push_deps_result(
                    items=self._reconcile_rows(
                        note_type, base_field, rows, plan, classified
                    )
                ),
                on_failure=lambda exc: self._push_deps_result(
                    error=self._ctx.friendly(exc, "Classify dependencies failed")
                ),
                label="Omnia: classifying dependencies…",
            )
            return
        # Nothing new to classify (all refs known, or no refs) — reconcile on the main thread so
        # a vanished derived edge is still dropped, no LLM call needed.
        self._push_deps_result(
            items=self._reconcile_rows(note_type, base_field, rows, plan, {})
        )

    def _plan_dep_classification(
        self, note_type: str, base_field: str, rows: list[Any]
    ) -> _DepPlan:
        """Decide, per row, whether its refs need a (fresh) classify vs. a memo hit.

        ALL of a row's refs are (re)classified (the prompt is the source of truth for hard/soft),
        so a row goes to the LLM (``uncached_items``) unless its exact prompt was already classified
        this session (memo hit → ``cached``). See :class:`_DepPlan`.
        """
        from omnia.plugins.smart_notes.engine.interpolation import extract_field_refs

        uncached_items: list[tuple[str, str, list[str]]] = []
        cached: dict[str, dict[str, str]] = {}
        for row in rows:
            field = str(row.get("field", ""))
            prompt = str(row.get("prompt", ""))
            # Classify ALL of the prompt's refs (not just newly-added ones): the prompt is the
            # source of truth for hard/soft, so an existing edge is re-coloured when the prompt's
            # semantics around it change. The per-prompt memo means an UNCHANGED prompt still
            # costs no LLM call.
            refs = extract_field_refs(prompt)
            if not refs:
                continue  # no field refs — nothing to classify (reconcile still drops vanished)
            memo = self._deps_memo.get((note_type, base_field, field, prompt))
            if memo is not None:
                cached[field] = memo
            else:
                uncached_items.append((field, prompt, refs))
        return _DepPlan(uncached_items=uncached_items, cached=cached)

    def _reconcile_rows(
        self,
        note_type: str,
        base_field: str,
        rows: list[Any],
        plan: _DepPlan,
        classified: dict[str, tuple[Any, ...]],
    ) -> list[dict[str, Any]]:
        """Reconcile every row's ``depends_on`` from the classifier verdicts (main thread).

        Folds the fresh batch ``classified`` (EdgeKinding tuples) with the plan's memo-``cached``
        verdicts, memoises the fresh ones by ``(note_type, base_field, field, prompt)``, and returns
        the recoloured
        per-field ``depends_on`` payload the page applies.
        """
        from omnia.gui.smart_notes.html import _deps_from_payload
        from omnia.plugins.smart_notes.engine.rules import reconcile_field_deps

        fresh = {
            field: {k.field: k.kind for k in kindings}
            for field, kindings in classified.items()
        }
        for field, kinds in fresh.items():
            prompt = next(
                (
                    str(r.get("prompt", ""))
                    for r in rows
                    if str(r.get("field", "")) == field
                ),
                "",
            )
            self._deps_memo[(note_type, base_field, field, prompt)] = kinds

        items: list[dict[str, Any]] = []
        for row in rows:
            field = str(row.get("field", ""))
            prompt = str(row.get("prompt", ""))
            current = _deps_from_payload(row.get("depends_on", []))
            classified_for_field = fresh.get(field) or plan.cached.get(field) or {}
            reconciled = reconcile_field_deps(prompt, classified_for_field, current)
            items.append(
                {
                    "field": field,
                    "depends_on": [
                        {"field": dep.field, "kind": dep.kind, "auto": dep.auto}
                        for dep in reconciled
                    ],
                }
            )
        return items

    def _push_deps_result(
        self, *, items: Optional[list[dict[str, Any]]] = None, error: str = ""
    ) -> None:
        """Send the reconciled per-field ``depends_on`` to ``window.__snDepsResult``.

        On error the page is sent an ``{error}`` item-list shape it ignores gracefully; the hook
        only applies entries with a ``field``.
        """
        payload: list[dict[str, Any]] | dict[str, Any]
        payload = {"error": error} if error else (items or [])
        self._ctx.eval_js(f"window.__snDepsResult({json.dumps(payload)});")

    def deps_map_for_rows(
        self, note_type: str, base_field: str, rows: list[Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """Classify + reconcile the given rows SYNCHRONOUSLY for the auto/improve fold.

        Called from INSIDE an existing ``run_in_background`` worker (auto-smart / improve-all),
        so the LLM call here is already off the main thread — it must NOT push to the page. It
        returns ``{field: [{field, kind, auto}]}`` for the affected fields, which the caller folds
        into its single pushed payload. The prompt is the source of truth for hard/soft: it
        classifies ALL refs and :func:`reconcile_field_deps` re-colours every edge to the fresh
        verdict (so the just-written auto-prompt / improved prompts drive the graph's colours).
        """
        from omnia.gui.smart_notes.html import _deps_from_payload
        from omnia.plugins.smart_notes.authoring import PromptAuthor
        from omnia.plugins.smart_notes.engine.interpolation import extract_field_refs
        from omnia.plugins.smart_notes.engine.rules import reconcile_field_deps

        items: list[tuple[str, str, list[str]]] = []
        for row in rows:
            prompt = str(row.get("prompt", ""))
            # Classify ALL refs so the fold re-colours existing edges to the prompt's semantics
            # (the prompt is the source of truth), not only genuinely-new refs.
            refs = extract_field_refs(prompt)
            items.append((str(row.get("field", "")), prompt, refs))
        hub = self._ctx.build_hub()
        if hub is None:
            return {}
        classified = PromptAuthor(hub.llm()).classify_dependencies_batch(
            note_type=note_type, base_field=base_field, items=items
        )
        fresh = {
            field: {k.field: k.kind for k in kindings}
            for field, kindings in classified.items()
        }
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            field = str(row.get("field", ""))
            prompt = str(row.get("prompt", ""))
            current = _deps_from_payload(row.get("depends_on", []))
            reconciled = reconcile_field_deps(prompt, fresh.get(field, {}), current)
            result[field] = [
                {"field": dep.field, "kind": dep.kind, "auto": dep.auto}
                for dep in reconciled
            ]
        return result

    # --- graph→prompt dependency sync (Feature 2) ------------------------------------
    def on_validate_prompt(self, data: dict[str, Any]) -> dict[str, Any]:
        """The popover's live guard rail: does a candidate prompt still derive the SAME edges?

        Feature 2 (graph → prompt): SYNCHRONOUS, no LLM — the diff popover's Now textarea calls
        this (debounced) on every keystroke to gate Apply. The candidate must derive EXACTLY the
        full intended dependency edge set at the node (all of B's edges, not just the changed
        one): we derive the intended set from ``intended_depends_on`` (its own ``{{refs}}`` are
        irrelevant — the deps fix the set + kinds) and diff the candidate's own ``{{refs}}``
        against it. A candidate that adds/removes a field ref, or breaks ``{{}}`` syntax, comes
        back ``ok=False`` so the page disables Apply.

        Args:
            data: ``{note_type, base_field, target_field, prompt, intended_depends_on:[{field,
                kind}]}``.

        Returns:
            ``{syntax_errors:[...], consistency:{ok, added_fields, removed_fields, kind_changes,
            messages}}``, or ``{error}`` on a malformed request.
        """
        from omnia.plugins.smart_notes.engine import NodeEdgeSet, validate_prompt_syntax

        try:
            target = str(data.get("target_field", ""))
            candidate = str(data.get("prompt", ""))
            intended = self._deps_from_payload_pairs(
                data.get("intended_depends_on", [])
            )
            known = anki_compat.note_type_field_names(str(data.get("note_type", "")))
            # The intended edge set: derive from a synthetic prompt that references every intended
            # dep (so the full intended REF set is captured) UNIONed with the deps (so each edge
            # carries its intended kind), mirroring PromptAuthor._guarded_rewrite's gate.
            synthetic = " ".join(f"{{{{{dep.field}}}}}" for dep in intended)
            before = NodeEdgeSet.derive(target, synthetic, intended, known)
            after = NodeEdgeSet.derive(target, candidate, [], known)
            result = before.diff(after)
            return {
                "syntax_errors": list(validate_prompt_syntax(candidate)),
                "consistency": {
                    "ok": result.ok,
                    "added_fields": list(result.added_fields),
                    "removed_fields": list(result.removed_fields),
                    "kind_changes": [list(kc) for kc in result.kind_changes],
                    "messages": list(result.messages),
                },
            }
        except Exception:  # boundary: a malformed payload must not crash the dialog
            logger.exception("smart_notes: validate_prompt failed")
            return {"error": "Could not validate the prompt — see logs."}

    def on_rewrite_edges(self, data: dict[str, Any]) -> None:
        """Rewrite each changed dependent node's prompt to reflect its graph edge change (off-thread).

        Feature 2 (graph → prompt): for EACH change the page posts, asks
        :meth:`PromptAuthor.rewrite_for_edge_change` to apply that one edit (guard-railed: the new
        prompt must derive the node's intended edge set, with one repair retry, else ``ok=False``
        and the old prompt). All changes run in ONE :func:`run_in_background`; the result is pushed
        from the SUCCESS callback (main thread) via ``window.__snRewriteResult`` — never inside the
        worker (off-thread ``eval_js`` hard-crashes Qt). The JS sends ONE change at a time for
        order-correctness, so this works for a single-element list.

        Args:
            data: ``{note_type, base_field, changes:[{target, old_prompt, kept_deps:[{field,kind}],
                change:{action,src,old_kind,new_kind}, intended_depends_on:[{field,kind}]}]}``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        changes = list(data.get("changes", []))
        if not changes:
            self._push_rewrite(results=[])
            return
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_rewrite(error="Provider config error — see logs.")
            return
        known = anki_compat.note_type_field_names(note_type)

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        def work() -> list[dict[str, Any]]:
            author = PromptAuthor(hub.llm())
            results: list[dict[str, Any]] = []
            for item in changes:
                target = str(item.get("target", ""))
                old_prompt = str(item.get("old_prompt", ""))
                kept_deps = self._deps_from_payload_pairs(item.get("kept_deps", []))
                intended = self._deps_from_payload_pairs(
                    item.get("intended_depends_on", [])
                )
                change = self._edge_change_from_payload(item.get("change", {}))
                rewrite = author.rewrite_for_edge_change(
                    note_type=note_type,
                    base_field=base_field,
                    target_field=target,
                    old_prompt=old_prompt,
                    kept_deps=kept_deps,
                    change=change,
                    known_fields=known,
                    intended_depends_on=intended,
                )
                results.append(
                    {
                        "field": target,
                        "old_prompt": rewrite.old_prompt or old_prompt,
                        "new_prompt": rewrite.prompt,
                        "ok": rewrite.ok,
                        "reason": rewrite.reason,
                    }
                )
            return results

        anki_compat.run_in_background(
            work,
            on_success=lambda results: self._push_rewrite(results=results),
            on_failure=lambda exc: self._push_rewrite(
                error=self._ctx.friendly(exc, "Rewrite failed")
            ),
            label="Omnia: rewriting prompts…",
        )

    def on_improve_prompt_pinned(self, data: dict[str, Any]) -> None:
        """Improve a prompt's wording inside the popover while PINNING its dependency set (off-thread).

        Feature 2 (graph → prompt): the popover's ✨ Improve button calls
        :meth:`PromptAuthor.improve_in_popover`, which may polish phrasing but must reference
        EXACTLY ``fixed_deps`` (no added/dropped ref). The result is pushed from the SUCCESS
        callback (main thread) via the DEDICATED ``window.__snDiffImproveResult`` hook (NOT the
        prompt editor's shared ``window.__snImproveResult``) — never inside the worker. The
        dedicated hook + the popover's own field-guard ensure a stale/discarded improve can never
        fall through to the editor path and write an UNVERIFIED prompt onto a row.

        Args:
            data: ``{note_type, base_field, target_field, prompt, fixed_deps:[{field,kind}]}``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("target_field", ""))
        prompt = str(data.get("prompt", ""))
        if not prompt.strip():
            self._push_improve_pinned(
                field, error="Write a prompt first, then Improve it."
            )
            return
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_improve_pinned(field, error="Provider config error — see logs.")
            return
        fixed_deps = self._deps_from_payload_pairs(data.get("fixed_deps", []))
        known = anki_compat.note_type_field_names(note_type)

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve_in_popover(
                note_type=note_type,
                base_field=base_field,
                target_field=field,
                prompt=prompt,
                fixed_deps=fixed_deps,
                known_fields=known,
            ),
            on_success=lambda rewrite: self._push_improve_pinned(
                field, prompt=rewrite.prompt, ok=rewrite.ok, reason=rewrite.reason
            ),
            on_failure=lambda exc: self._push_improve_pinned(
                field, error=self._ctx.friendly(exc, "Improve failed")
            ),
            label="Omnia: improving prompt…",
        )

    @staticmethod
    def _deps_from_payload_pairs(deps: Any) -> list[Any]:
        """Build :class:`FieldDep`s from a posted ``[{field, kind}]`` list (the popover shape)."""
        from omnia.gui.smart_notes.html import _deps_from_payload

        return _deps_from_payload(deps)

    @staticmethod
    def _edge_change_from_payload(change: Any) -> Any:
        """Build an :class:`EdgeChange` from the posted ``{action, src, old_kind, new_kind}`` dict."""
        from omnia.plugins.smart_notes.authoring import EdgeChange

        data = change if isinstance(change, dict) else {}
        return EdgeChange(
            action=str(data.get("action", "")),
            src=str(data.get("src", "")),
            old_kind=str(data.get("old_kind", "")),
            new_kind=str(data.get("new_kind", "")),
        )

    def _push_rewrite(
        self, *, results: Optional[list[dict[str, Any]]] = None, error: str = ""
    ) -> None:
        """Send the edge-rewrite outcome to ``window.__snRewriteResult``.

        On success a list of ``{field, old_prompt, new_prompt, ok, reason}`` (one per change); on
        error an ``{error}`` object the page surfaces on the graph toast.
        """
        payload: list[dict[str, Any]] | dict[str, Any]
        payload = {"error": error} if error else (results or [])
        self._ctx.eval_js(f"window.__snRewriteResult({json.dumps(payload)});")

    def _push_improve_pinned(
        self,
        field: str,
        *,
        prompt: str = "",
        ok: bool = True,
        reason: str = "",
        error: str = "",
    ) -> None:
        """Send a pinned-improve outcome to the DEDICATED ``window.__snDiffImproveResult`` hook.

        Kept separate from the prompt editor's ``window.__snImproveResult`` so a stale/discarded
        pinned improve can never fall through to the editor path and write an unverified prompt
        onto a row (W1). Carries ``ok``/``reason`` so the popover can surface a guard-rail failure;
        the popover ignores it unless it is still open on this ``field``.
        """
        result: dict[str, Any] = (
            {"error": error}
            if error
            else {"prompt": prompt, "ok": ok, "reason": reason}
        )
        self._ctx.eval_js(
            f"window.__snDiffImproveResult({json.dumps(field)}, {json.dumps(result)});"
        )
