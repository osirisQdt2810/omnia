"""Pure HTML/CSS/JS builder + row↔config mapping for the Smart Notes config page.

The Smart Notes dialog (``dialog.py``) is thin Qt/webview glue; this module assembles the
page from asset files under the sibling ``web/`` folder (``web/page.html`` / ``web/page.css``
and the ordered ``web/0N-*.js`` script pieces) and holds the pure mapping between the
note-type config model and the table rows, so both unit-test headless.
Everything is inlined into one document (no external <link>/<script src>) because the host
:class:`~omnia.gui.web_dialog.WebDialog` applies a strict CSP.

The page is note-type-centric: one always-present BASE (input) field that is never
generated, and one table row per other field describing how to generate it. It talks back to
Python through the WebDialog bridge with these ops:

* ``list_note_types`` → ``[name, ...]``
* ``load`` ``{note_type}`` → the note type's base field, all fields, rows, providers, decks
* ``set_base_field`` ``{note_type, base_field}`` → re-rendered rows for the new base
* ``create_field`` ``{note_type, field_name}`` → the note type's updated field names
* ``auto_smart`` ``{note_type, base_field, rows, decks}`` → rows with prompts/types filled in
  (pushed via ``window.__snAutoResult``, optionally carrying a ``deps`` map alongside ``rows``)
* ``improve_prompt`` ``{note_type, base_field, field, prompt}`` → a polished prompt (pushed)
* ``improve_all`` ``{note_type, base_field, rows, decks}`` → ``{improved: {field: prompt}}``
  (pushed via ``window.__snImproveAllResult``, optionally carrying a ``deps`` map)
* ``classify_deps`` ``{note_type, base_field, rows:[{field, prompt, depends_on}]}`` → the
  prompt→graph sync: off-thread, classifies each row's NEW refs hard/soft and reconciles, then
  pushes ``window.__snDepsResult([{field, depends_on:[{field, kind, auto}]}])``
* ``validate_prompt`` ``{note_type, base_field, target_field, prompt, intended_depends_on:
  [{field, kind}]}`` → the graph→prompt popover's live guard rail: SYNCHRONOUS, no LLM, returns
  ``{syntax_errors:[...], consistency:{ok, added_fields, removed_fields, kind_changes,
  messages}}`` — the candidate must derive the SAME edge set as the full intended deps
* ``rewrite_edges`` ``{note_type, base_field, changes:[{target, old_prompt, kept_deps,
  change:{action,src,old_kind,new_kind}, intended_depends_on}]}`` → the graph→prompt sync:
  off-thread, rewrites each changed node's prompt (guard-railed), pushes
  ``window.__snRewriteResult([{field, old_prompt, new_prompt, ok, reason}])``
* ``improve_prompt_pinned`` ``{note_type, base_field, target_field, prompt, fixed_deps}`` → the
  popover's ✨ Improve: off-thread, polishes wording while PINNING the dep set, pushes
  ``window.__snImproveResult(field, {prompt, ok, reason})`` (shared with the prompt editor)
* ``preview`` ``{note_type, base_field, field, type, prompt, provider, model, voice}`` → a
  generated sample for a random note (pushed)
* ``account_data`` → ``{models: {kind: [...]}, defaults: {kind: {provider, model}}}``
* ``set_default_model`` ``{kind, provider, model}`` → ``{defaults}`` (the central default used
  for detect-language / Auto-prompt / Improve and any "(inherit)" field)
* ``account_keys`` → ``{providers: [card]}`` (managed providers' credential fields + state)
* ``set_secret`` ``{provider, field, value}`` → ``{ok}`` (persist one credential field)
* ``browse_file`` ``{provider, field}`` → ``{path}`` (native picker; persists the chosen path)
* ``open_url`` ``{url}`` (open a provider console/billing link in the browser)
* ``account_keys_credit`` → OpenRouter balance pushed via ``window.__snKeysCreditResult``
* ``save`` ``{note_type, base_field, rows, decks, options}`` → ``{ok: true}`` once persisted

The Provider/Model/Voice dropdowns are driven by a catalog baked into ``window.__SN_CATALOG``
(see :func:`~omnia.core.providers.catalog.catalog_payload`); the ``load`` response's
``providers`` list is kept for back-compat but the page no longer needs it.

This module imports only pure data (the config models + the provider catalog); nothing from
``aqt``/``anki``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from omnia.core.providers.catalog import catalog_payload
from omnia.gui.assets import read_asset, read_assets
from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)

if TYPE_CHECKING:
    from omnia.core.providers.native_runtime import NativeRuntimeManager

_FIELD_TYPES = ("text", "tts", "image")
_DEP_KINDS = ("hard", "soft")

# The page script is split into cohesive pieces under ``web/``; they are concatenated in this
# exact order (a single IIFE opened by 01 and closed by 07).
_PAGE_JS_PARTS = [
    "01-bridge.js",
    "02-catalog.js",
    "03-render.js",
    "04-modal.js",
    "05-handlers.js",
    "06-graph.js",
    "07-init.js",
]


def native_runtimes_payload(manager: NativeRuntimeManager) -> dict[str, Any]:
    """Build the Options → General "Native runtimes" panel data, grouped by section.

    Walks the process-wide native-runtime registry (grouped by section) and, for each spec,
    reports its identity, download-size hint, and whether ``manager`` already has it installed.
    Pure: takes the manager (its only Anki-touching collaborator) so it unit-tests with a fake
    manager + the registry, no venv/pip/network.

    Args:
        manager: The native-runtime manager whose install state is queried per spec.

    Returns:
        ``{"sections": [{"section": str, "runtimes": [{name, label, size_hint, section,
        installed}, ...]}, ...]}`` in the registry's deterministic section/name order.
    """
    from omnia.core.providers.native_runtime import native_runtimes_by_section

    sections: list[dict[str, Any]] = []
    for section, specs in native_runtimes_by_section().items():
        sections.append(
            {
                "section": section,
                "runtimes": [
                    {
                        "name": spec.name,
                        "label": spec.label,
                        "size_hint": spec.size_hint,
                        "section": spec.section,
                        "installed": manager.is_installed(spec),
                    }
                    for spec in specs
                ],
            }
        )
    return {"sections": sections}


def set_native_runtime(
    manager: NativeRuntimeManager,
    name: str,
    enabled: bool,
    *,
    run_async: Callable[
        [Callable[[], None], Callable[[], None], Callable[[Exception], None]], None
    ],
    push_progress: Callable[[str, str], None],
    push_done: Callable[[str, bool, str], None],
) -> dict[str, Any] | None:
    """Route an install (enabled) / uninstall (disabled) toggle for one native runtime.

    Pure decision logic for ``set_native_runtime``, with its Anki-touching collaborators
    injected so it unit-tests headless (a fake manager + a synchronous ``run_async``):

    * Unknown ``name`` → push a done-error and return None.
    * ``enabled`` → schedule ``manager.ensure_installed`` through ``run_async`` (it is slow —
      venv + pip), forwarding progress lines via ``push_progress`` and the final outcome
      (success or failure) via ``push_done``; return None (the row updates through the pushes).
    * not ``enabled`` → call ``manager.uninstall`` synchronously and return the refreshed row
      payload (``{name, installed}``) for the immediate JS callback.

    Args:
        manager: The native-runtime manager (its install/uninstall are the side effects).
        name: The runtime name from the page.
        enabled: True to install, False to uninstall.
        run_async: Runs ``op`` off-thread, then ``on_success`` / ``on_failure`` (the install
            seam). The dialog backs this with ``anki_compat.run_in_background``.
        push_progress: ``(name, message)`` → a progress line for the row.
        push_done: ``(name, installed, error)`` → the final install outcome for the row.

    Returns:
        The synchronous uninstall payload, or None when the action is asynchronous/unknown.
    """
    from omnia.core.providers.native_runtime import native_runtime

    spec = native_runtime(name)
    if spec is None:
        push_done(name, False, "Unknown runtime.")
        return None

    if not enabled:
        manager.uninstall(spec)
        return {"name": name, "installed": False}

    def op() -> None:
        manager.ensure_installed(spec, on_progress=lambda msg: push_progress(name, msg))

    run_async(
        op,
        lambda: push_done(name, True, ""),
        lambda exc: push_done(name, False, _native_error(exc)),
    )
    return None


def _native_error(exc: Exception) -> str:
    """A short user-facing message for a native-runtime install failure.

    A :class:`ProviderError` already carries an actionable message (e.g. "no host Python …"),
    so it is shown verbatim; anything else is generic.
    """
    from omnia.core.providers.errors import ProviderError

    if isinstance(exc, ProviderError):
        return f"Install failed: {exc}"
    return "Install failed — see logs."


def rows_for_note_type(
    config: SmartNotesNoteTypeConfig | None,
    all_fields: list[str],
    base_field: str,
) -> list[SmartNotesFieldConfig]:
    """Return one :class:`SmartNotesFieldConfig` per NON-base field, merging saved + live.

    Every current field of the note type except ``base_field`` gets a row: a previously saved
    row is reused as-is (preserving the user's prompt/type/overrides), and a field with no
    saved row appears with defaults. Saved rows whose field no longer exists on the note type
    are dropped, and a saved row matching the base field is excluded. Order follows the note
    type's live field order so the table mirrors the editor.

    Args:
        config: The saved note-type config, or None when the note type has none yet.
        all_fields: The note type's current field names, in order.
        base_field: The designated base (input) field, never generated.

    Returns:
        The per-field rows the table renders, in ``all_fields`` order.
    """
    saved = {row.field: row for row in config.fields} if config is not None else {}
    rows: list[SmartNotesFieldConfig] = []
    for name in all_fields:
        if name == base_field:
            continue
        existing = saved.get(name)
        rows.append(
            existing.copy()
            if existing is not None
            else SmartNotesFieldConfig(field=name)
        )
    return rows


def resolve_base_field(
    config: SmartNotesNoteTypeConfig | None, all_fields: list[str]
) -> str:
    """Return the base field to show: the saved one (if still present) else the first field."""
    if config is not None and config.base_field in all_fields:
        return config.base_field
    return all_fields[0] if all_fields else ""


def field_configs_from_payload(
    rows: list[dict[str, object]],
) -> list[SmartNotesFieldConfig]:
    """Build :class:`SmartNotesFieldConfig`s from the JS-posted row dicts (one per non-base field).

    Each dict carries the row's editable state (``field``, ``enabled``, ``type``, ``prompt``,
    ``prompt_locked``, ``provider``, ``model``, ``voice``, ``language``, ``overwrite``,
    ``depends_on``). A row with no ``field`` name is skipped; an invalid ``type`` falls back to
    ``"text"`` so a malformed payload can't raise during validation.

    Args:
        rows: The row dicts posted from the page.

    Returns:
        Validated field configs, ready to assemble into a note-type config.
    """
    configs: list[SmartNotesFieldConfig] = []
    for row in rows:
        name = str(row.get("field", "")).strip()
        if not name:
            continue
        field_type = str(row.get("type", "text"))
        if field_type not in _FIELD_TYPES:
            field_type = "text"
        configs.append(
            SmartNotesFieldConfig(
                field=name,
                enabled=bool(row.get("enabled", False)),
                type=field_type,
                prompt=str(row.get("prompt", "")),
                prompt_locked=bool(row.get("prompt_locked", False)),
                provider=str(row.get("provider", "")),
                model=str(row.get("model", "")),
                voice=str(row.get("voice", "")),
                language=str(row.get("language", "")),
                overwrite=bool(row.get("overwrite", False)),
                depends_on=_deps_from_payload(row.get("depends_on", [])),
            )
        )
    return configs


def _deps_from_payload(deps: object) -> list[FieldDep]:
    """Build the explicit dependency edges from a row's posted ``depends_on`` list.

    Each entry is ``{field, kind, auto}``; an entry with no ``field`` name is skipped and an
    invalid ``kind`` falls back to ``"hard"`` so a malformed payload can't raise during
    validation. The ``auto`` provenance flag (a classifier-written edge) round-trips so the page
    preserving it through ``collectRows``/``readDependsOn`` keeps a classifier kind from being
    downgraded to a user edge on the next save.

    Args:
        deps: The ``depends_on`` value posted from the page (expected: a list of dicts).

    Returns:
        Validated dependency edges, in posted order.
    """
    result: list[FieldDep] = []
    if not isinstance(deps, list):
        return result
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        field = str(dep.get("field", "")).strip()
        if not field:
            continue
        kind = str(dep.get("kind", "hard"))
        if kind not in _DEP_KINDS:
            kind = "hard"
        result.append(
            FieldDep(field=field, kind=kind, auto=bool(dep.get("auto", False)))
        )
    return result


def note_type_config_from_payload(
    note_type: str,
    base_field: str,
    rows: list[dict[str, object]],
    decks: list[int] | None = None,
) -> SmartNotesNoteTypeConfig:
    """Assemble a :class:`SmartNotesNoteTypeConfig` from the posted note type, base, rows, decks."""
    return SmartNotesNoteTypeConfig(
        note_type=note_type,
        base_field=base_field,
        fields=field_configs_from_payload(rows),
        decks=[int(d) for d in (decks or [])],
    )


def merge_note_type_into(
    note_types: list[SmartNotesNoteTypeConfig], updated: SmartNotesNoteTypeConfig
) -> list[SmartNotesNoteTypeConfig]:
    """Return ``note_types`` with ``updated`` replacing its same-name entry (or appended).

    Used by the save handler: the dialog edits one note type at a time, so persisting it must
    merge that note type into the existing list without disturbing the others.
    """
    merged = [nt for nt in note_types if nt.note_type != updated.note_type]
    merged.append(updated)
    return merged


def row_to_payload(row: SmartNotesFieldConfig) -> dict[str, object]:
    """Serialize one field config to the dict the page consumes (kept in sync with the JS)."""
    return {
        "field": row.field,
        "enabled": row.enabled,
        "type": row.type,
        "prompt": row.prompt,
        "prompt_locked": row.prompt_locked,
        "provider": row.provider,
        "model": row.model,
        "voice": row.voice,
        "language": row.language,
        "overwrite": row.overwrite,
        "depends_on": [
            {"field": dep.field, "kind": dep.kind, "auto": dep.auto}
            for dep in row.depends_on
        ],
    }


def cycle_error_for_config(config: SmartNotesNoteTypeConfig) -> str:
    """Return a user-facing message if ``config``'s field dependencies form a cycle, else ``""``.

    The persistence backstop for the graph→prompt sync (Feature 2): the client-side
    ``would_create_cycle`` precheck on Apply is complete given a fresh graph, but a cyclic
    prompt+deps could still land in a row via any bug / stale graph. The save path calls this so a
    cyclic config is never persisted. Pure: builds the effective :class:`FieldGraph` and runs its
    :meth:`~omnia.plugins.smart_notes.engine.graph.FieldGraph.validate_acyclic` guard (over edges
    of both kinds), so it unit-tests headless.

    Args:
        config: The note type's smart-notes config (with each field's ``depends_on``).

    Returns:
        A ready-to-show error string when a cycle exists, or ``""`` when the graph is a DAG.
    """
    from omnia.plugins.smart_notes.engine.graph import FieldGraph
    from omnia.plugins.smart_notes.engine.ordering import SmartNotesCycleError

    try:
        FieldGraph.from_config(config).validate_acyclic()
    except SmartNotesCycleError as exc:
        return f"Can't save — the field dependencies form a cycle: {exc}."
    return ""


def graph_payload(config: SmartNotesNoteTypeConfig) -> dict[str, object]:
    """Build the field dependency graph payload (nodes + edges + geometry) the canvas renders.

    Computes the effective graph for ``config`` (:meth:`FieldGraph.from_config`), the
    deterministic layered layout (:meth:`FieldGraph.laid_out`, for the integer ``column``/``row``
    kept for back-compat), and the balanced grid-wrapped pixel geometry
    (:meth:`FieldGraph.flow_layout`), then serializes them for the page. Layout is the single
    source of truth — always computed in Python so the JS never re-implements longest-path; the
    page prefers each node's pixel ``x``/``y`` and frames the canvas from ``bounds``.

    Args:
        config: The note type's smart-notes config (with each field's ``depends_on``).

    Returns:
        ``{"nodes": [{name, is_base, generatable, column, row, x, y, w, h, lane}, ...],
        "edges": [{src, dst, kind, derived}, ...], "bounds": {width, height}}``.
    """
    from omnia.plugins.smart_notes.engine.graph import FieldGraph

    graph = FieldGraph.from_config(config)
    laid = graph.laid_out()
    flow = graph.flow_layout()
    geometry = {layout.name: layout for layout in flow.nodes}
    return {
        "nodes": [
            {
                "name": node.name,
                "is_base": node.is_base,
                "generatable": node.generatable,
                "column": node.column,
                "row": node.row,
                "x": geometry[node.name].x,
                "y": geometry[node.name].y,
                "w": geometry[node.name].w,
                "h": geometry[node.name].h,
                "lane": geometry[node.name].lane,
            }
            for node in laid.nodes
        ],
        "edges": [
            {
                "src": edge.src,
                "dst": edge.dst,
                "kind": edge.kind,
                "derived": edge.derived,
            }
            for edge in laid.edges
        ],
        "bounds": {"width": flow.width, "height": flow.height},
    }


def load_payload(
    note_type: str,
    config: SmartNotesNoteTypeConfig | None,
    all_fields: list[str],
    providers: list[str],
    all_decks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build the ``load`` op's response: base field, fields, rows, providers, and decks.

    ``all_decks`` is the full ``[{id, name}, ...]`` deck list for the picker; ``decks`` is the
    config's selected deck-id subset ([] = all decks).
    """
    base_field = resolve_base_field(config, all_fields)
    rows = rows_for_note_type(config, all_fields, base_field)
    return {
        "note_type": note_type,
        "base_field": base_field,
        "all_fields": all_fields,
        "rows": [row_to_payload(row) for row in rows],
        "providers": providers,
        "decks": list(config.decks) if config else [],
        "all_decks": all_decks or [],
        "graph": graph_payload(
            note_type_config_from_payload(
                note_type, base_field, [row_to_payload(row) for row in rows]
            )
        ),
    }


def build_smart_notes_html(
    *,
    dark: bool,
    init: dict[str, object] | None = None,
    catalog: dict[str, object] | None = None,
) -> str:
    """Build the full Smart Notes config page HTML, with the initial data baked in.

    The selectors + first note type's rows are seeded from ``init`` (``window.__SN_INIT``) so
    the page renders fully populated on load WITHOUT an init ``pycmd`` callback — Anki's bridge
    callback channel isn't ready the instant the page's inline script runs, so an init
    ``list_note_types``/``load`` round-trip is dropped and the dialog comes up blank. User
    actions (change note type, set base, create field, auto-smart, save) happen later, when the
    bridge is ready, so they keep using ``pycmd``. The provider/model/voice catalog is baked
    into ``window.__SN_CATALOG`` so the kind-aware dropdowns work without a callback too.

    Args:
        dark: Render the dark palette (Anki night mode) when True, else the light palette.
        init: ``{note_types, note_type, base_field, all_fields, rows, providers}`` for the
            initially-selected note type. None/empty falls back to a JS ``list_note_types``.
        catalog: The provider/model/voice catalog (``window.__SN_CATALOG``). Defaults to the
            real :func:`~omnia.core.providers.catalog.catalog_payload`.

    Returns:
        A complete, self-contained HTML document string.
    """
    return read_asset(__file__, "web", "page.html").format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=read_asset(__file__, "web", "page.css"),
        types_json=json.dumps(_FIELD_TYPES),
        init_json=json.dumps(init) if init else "null",
        catalog_json=json.dumps(catalog if catalog is not None else catalog_payload()),
        js=read_assets(__file__, "web", names=_PAGE_JS_PARTS),
    )


# Re-exported for the dialog so it doesn't duplicate the literal anywhere.
FIELD_TYPES = _FIELD_TYPES
