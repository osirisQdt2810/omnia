"""The authoring controller: ✨ Auto-prompt, ✦ Improve (one + all), ▶ Preview.

The three off-thread LLM authoring actions: Auto-prompt writes a prompt+type for every
Generate-on + unlocked field; Improve rewrites a rough prompt into a polished one (per field
and "Improve all"); Preview generates a sample for one field against a real (or fabricated)
note. Each pushes its result back through a page hook from the ``run_in_background`` success
callback (never the worker — off-main ``eval_js`` hard-crashes Qt).

Auto-prompt and Improve-all fold in the Feature-1 prompt→graph classify so the new/improved
prompts' refs are coloured hard/soft in the graph; that one cross-controller call goes through
the injected :class:`~omnia.gui.smart_notes.dialogs.controllers.graph.GraphController`. Only loaded
inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core import anki_compat
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.gui.smart_notes.dialogs.controllers.graph import GraphController
from omnia.gui.smart_notes.html import note_type_config_from_payload, row_to_payload

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
    )
    from omnia.plugins.smart_notes.engine import GenerationResult


def _truncate(value: str, limit: int = 120) -> str:
    """Collapse whitespace and cap length so a long field body can't bloat the preview payload."""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AuthoringController:
    """Auto-prompt / improve / preview ops.

    Args:
        ctx: The shared service context.
        graph: The graph controller, for the Feature-1 prompt→graph classify fold (the only
            cross-controller dependency).
    """

    def __init__(self, ctx: SmartNotesContext, graph: GraphController) -> None:
        self._ctx = ctx
        self._graph = graph

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "auto_smart": self.on_auto_smart,
            "improve_prompt": self.on_improve_prompt,
            "improve_all": self.on_improve_all,
            "preview": self.on_preview,
        }

    def on_auto_smart(self, data: dict[str, Any]) -> None:
        """Run Auto-prompt off the Qt main thread; push the result back via the page hook.

        Returns None immediately — the LLM call can't block the main thread, so the new rows
        are delivered to the page through ``window.__snAutoResult`` once the background op
        finishes (success or a friendly ProviderError message). Reports a clear, actionable
        message when there is nothing to fill (no Generate-on + unlocked field) instead of
        silently succeeding. After the prompts are written it folds in the SAME batched
        prompt→graph classify (Feature 1) so the new prompts' refs are coloured hard/soft in the
        graph — done in the worker (already off-thread), pushed once from the success callback.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        config = note_type_config_from_payload(
            note_type,
            base_field,
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )

        from omnia.plugins.smart_notes.authoring import PromptAuthor, candidate_fields

        candidates = candidate_fields(config)
        if not candidates:
            self._push_auto_result(
                error="Nothing to fill — switch Generate on (and unlock) for at least one "
                "field, then run Auto-prompt."
            )
            return
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_auto_result(error="Provider config error — see logs.")
            return

        def work() -> tuple[Any, dict[str, list[dict[str, Any]]]]:
            updated = PromptAuthor(hub.llm()).auto_smart(config)
            rows = [row_to_payload(row) for row in updated.fields]
            deps = self._graph.deps_map_for_rows(note_type, base_field, rows)
            return updated, deps

        anki_compat.run_in_background(
            work,
            on_success=lambda result: self._push_auto_result(
                config=result[0], filled=len(candidates), deps=result[1]
            ),
            on_failure=self._on_auto_failure,
            label="Omnia: auto-prompt…",
        )

    def on_improve_prompt(self, data: dict[str, Any]) -> None:
        """Rewrite ONE field's rough prompt into a polished one (off-thread; pushed back).

        Mechanism X: the user types a short/rough request and clicks ✨ Improve in the prompt
        editor; the result returns through ``window.__snImproveResult``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        rough = str(data.get("prompt", ""))
        if not rough.strip():
            self._push_improve(
                field, error="Type a rough prompt first, then Improve it."
            )
            return
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_improve(field, error="Provider config error — see logs.")
            return
        other_fields = [
            name
            for name in anki_compat.note_type_field_names(note_type)
            if name != field
        ]

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve(
                note_type=note_type,
                base_field=base_field,
                target_field=field,
                rough=rough,
                other_fields=other_fields,
            ),
            on_success=lambda text: self._push_improve(field, prompt=text),
            on_failure=lambda exc: self._push_improve(
                field, error=self._ctx.friendly(exc, "Improve failed")
            ),
            label="Omnia: improving prompt…",
        )

    def on_improve_all(self, data: dict[str, Any]) -> None:
        """Rewrite every Generate-on + unlocked field's rough prompt at once (off-thread; pushed).

        The result returns through ``window.__snImproveAllResult`` as ``{field: prompt}`` plus an
        optional ``deps`` map. After the prompts are rewritten it folds in the SAME batched
        prompt→graph classify (Feature 1) against the IMPROVED prompts so the graph recolours —
        run in the worker (already off-thread), pushed once from the success callback.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        posted_rows = list(data.get("rows", []))
        items = [
            (str(row.get("field", "")), str(row.get("prompt", "")))
            for row in posted_rows
            if row.get("enabled")
            and not row.get("prompt_locked")
            and str(row.get("field", "")) != base_field
            and str(row.get("prompt", "")).strip()
        ]
        if not items:
            self._push_improve_all(
                error="No field has a prompt to improve — switch Generate on (and unlock) a "
                "field with a prompt first."
            )
            return
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_improve_all(error="Provider config error — see logs.")
            return

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        def work() -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
            improved = PromptAuthor(hub.llm()).improve_all(
                note_type=note_type, base_field=base_field, items=items
            )
            # Classify against the IMPROVED prompts (keeping each row's current depends_on).
            rows = [
                {
                    "field": str(row.get("field", "")),
                    "prompt": improved.get(
                        str(row.get("field", "")), str(row.get("prompt", ""))
                    ),
                    "depends_on": row.get("depends_on", []),
                }
                for row in posted_rows
                if str(row.get("field", "")) in improved
            ]
            deps = self._graph.deps_map_for_rows(note_type, base_field, rows)
            return improved, deps

        anki_compat.run_in_background(
            work,
            on_success=lambda result: self._push_improve_all(
                improved=result[0], deps=result[1]
            ),
            on_failure=lambda exc: self._push_improve_all(
                error=self._ctx.friendly(exc, "Improve all failed")
            ),
            label="Omnia: improving prompts…",
        )

    def on_preview(self, data: dict[str, Any]) -> None:
        """Generate a sample for one field against a real (or fabricated) note (off-thread).

        Lets the user test a prompt before saving; the result returns through
        ``window.__snPreviewResult``.
        """
        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine import GenerationService

        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        kind = str(data.get("type", "text"))
        prompt = str(data.get("prompt", ""))
        rule = SmartNotesFieldRule(
            note_type=note_type,
            # Mirror compile_note_type_rules: with no prompt the base field is the source.
            source_field="" if prompt else base_field,
            target_field=field,
            kind=kind,
            prompt=prompt,
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            voice=str(data.get("voice", "")),
            language=str(data.get("language", "")),
        )
        fields = self._preview_fields(note_type, base_field)
        # The input fields (+ sample values) this preview reads, so the result shows WHAT it ran
        # against — not only the generated output. Computed here (main thread) and echoed on success.
        inputs = self._preview_inputs(rule, fields)
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_preview(field, error="Provider config error — see logs.")
            return
        service = GenerationService(hub)

        anki_compat.run_in_background(
            lambda: service.generate(rule, fields),
            on_success=lambda result: self._push_preview(
                field, result=result, inputs=inputs
            ),
            on_failure=lambda exc: self._push_preview(
                field, error=self._ctx.friendly(exc, "Preview failed")
            ),
            label="Omnia: preview…",
        )

    def _preview_inputs(
        self, rule: SmartNotesFieldRule, fields: dict[str, str]
    ) -> list[dict[str, str]]:
        """The input fields a preview reads (the prompt's ``{{refs}}``, or the source/base field
        when there is no prompt), paired with their sample values from :meth:`_preview_fields`.

        Reuses :func:`rule_source_fields` (the same "what does this field read" util the graph and
        ordering use) so the shown inputs exactly match the real dependency set. Values are looked
        up case-insensitively (Anki field names are) and truncated.
        """
        from omnia.plugins.smart_notes.engine.rules import rule_source_fields

        lower = {name.strip().lower(): value for name, value in fields.items()}
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for name in rule_source_fields(rule):
            key = name.strip().lower()
            if key in seen:  # a prompt may reference the same field twice — show it once
                continue
            seen.add(key)
            value = fields.get(name)
            if value is None:
                value = lower.get(key, "")
            out.append({"field": name, "value": _truncate(str(value))})
        return out

    def _preview_fields(self, note_type: str, base_field: str) -> dict[str, str]:
        """The field values a preview runs against.

        Uses the FIRST existing note of ``note_type`` when there is one; otherwise fabricates a
        sample (all fields blank). Either way, a blank base field is seeded with a sample word —
        most prompts self-guard to output nothing for an empty base, which is exactly the
        "(empty result)" the preview was hitting, so the seed makes the preview meaningful.
        """
        note = anki_compat.random_note_of_type(note_type or None)
        if note is not None:
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        else:
            fields = {name: "" for name in anki_compat.note_type_field_names(note_type)}
        if base_field and not str(fields.get(base_field, "")).strip():
            fields[base_field] = "example"
        return fields

    def _on_auto_failure(self, exc: Exception) -> None:
        self._push_auto_result(error=self._ctx.friendly(exc, "Auto-prompt failed"))

    def _push_auto_result(
        self,
        *,
        config: Optional[SmartNotesNoteTypeConfig] = None,
        filled: int = 0,
        deps: Optional[dict[str, list[dict[str, Any]]]] = None,
        error: str = "",
    ) -> None:
        """Send the Auto-smart outcome to the page's ``window.__snAutoResult`` hook.

        Carries the filled ``rows`` and, when the prompt→graph fold ran, an optional ``deps`` map
        (``{field: [{field, kind, auto}]}``) the page applies per-field to recolour the graph.
        """
        if error:
            result: dict[str, Any] = {"error": error}
        else:
            assert config is not None
            result = {
                "rows": [row_to_payload(row) for row in config.fields],
                "filled": filled,
            }
            if deps:
                result["deps"] = deps
        self._ctx.eval_js(f"window.__snAutoResult({json.dumps(result)});")

    def _push_improve(self, field: str, *, prompt: str = "", error: str = "") -> None:
        """Send one Improve outcome to the page's ``window.__snImproveResult`` hook."""
        result: dict[str, Any] = {"error": error} if error else {"prompt": prompt}
        self._ctx.eval_js(
            f"window.__snImproveResult({json.dumps(field)}, {json.dumps(result)});"
        )

    def _push_improve_all(
        self,
        *,
        improved: Optional[dict[str, str]] = None,
        deps: Optional[dict[str, list[dict[str, Any]]]] = None,
        error: str = "",
    ) -> None:
        """Send the Improve-all outcome to the page's ``window.__snImproveAllResult`` hook.

        Carries the ``{field: prompt}`` map and, when the prompt→graph fold ran, an optional
        ``deps`` map (``{field: [{field, kind, auto}]}``) the page applies per-field.
        """
        if error:
            result: dict[str, Any] = {"error": error}
        else:
            result = {"improved": improved or {}}
            if deps:
                result["deps"] = deps
        self._ctx.eval_js(f"window.__snImproveAllResult({json.dumps(result)});")

    def _push_preview(
        self,
        field: str,
        *,
        result: Optional[GenerationResult] = None,
        inputs: Optional[list[dict[str, str]]] = None,
        error: str = "",
    ) -> None:
        """Send a Preview outcome to the page's ``window.__snPreviewResult`` hook.

        Text previews carry the rendered HTML; audio is played here and reported as a note;
        an image is reported as generated (not inserted — this is only a preview). On success the
        payload also carries ``inputs`` (``[{field, value}]``) — the fields the preview read — so
        the page can show what it ran against. Error paths carry no inputs.
        """
        if error:
            payload: dict[str, Any] = {"error": error}
        elif result is None:
            payload = {"error": "Preview produced no result."}
        else:
            payload = self._ctx.result_payload(result)
            payload["inputs"] = inputs or []
        self._ctx.eval_js(
            f"window.__snPreviewResult({json.dumps(field)}, {json.dumps(payload)});"
        )
