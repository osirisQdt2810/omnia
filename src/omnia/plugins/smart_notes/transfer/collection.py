"""Reading a bundle out of a collection, and applying one into it.

The only module in :mod:`~omnia.plugins.smart_notes.transfer` that touches Anki. It takes the
collection and the tool store as arguments rather than reaching for ``mw``, so the same code
runs under the GUI, under a script against a closed collection, and under a test with a fake.

Applying a bundle is deliberately a two-step: :func:`plan_import` decides what WOULD happen and
returns it for the user to look at, and :func:`apply_bundle` carries out a plan. An import can
rewrite prompts, drop rules and overwrite a note type's templates — none of which should first
become visible after the fact.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Optional

from omnia.plugins.smart_notes.config import SmartNotesNoteTypeConfig
from omnia.plugins.smart_notes.transfer.bundle import (
    USER_TOOL_PREFIX,
    BundleSource,
    NoteTypeBundle,
)
from omnia.plugins.smart_notes.transfer.remap import (
    RemapReport,
    remap_note_type_config,
    suggest_renames,
)

#: The collection-config key the Smart Notes settings live under (ADR-006).
SMART_NOTES_KEY = "omnia:smart_notes"

#: What an import may do about a name that already exists on the target.
MODE_CREATE = "create"  # no such note type here — make it from the bundle's schema
MODE_CLONE = "clone"  # there is one — make a SECOND note type under a new name
MODE_OVERWRITE = (
    "overwrite"  # there is one — reuse it, mapping the bundle's fields onto its
)


class TransferError(RuntimeError):
    """An export or import could not proceed (message is safe to show the user)."""


@dataclass
class ImportPlan:
    """What an import would do, computed before anything is written."""

    mode: str
    target_note_type: str
    renames: dict[str, str] = dataclass_field(default_factory=dict)
    unmapped_source_fields: list[str] = dataclass_field(default_factory=list)
    unused_target_fields: list[str] = dataclass_field(default_factory=list)
    creates_note_type: bool = False
    replaces_config: bool = False
    tools_to_install: list[str] = dataclass_field(default_factory=list)
    missing_tools: list[str] = dataclass_field(default_factory=list)
    missing_decks: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)


@dataclass
class ImportResult:
    """What an import actually did."""

    note_type: str
    created_note_type: bool
    fields_configured: int
    tools_written: list[str] = dataclass_field(default_factory=list)
    remap: Optional[RemapReport] = None


# --- reading the collection ---------------------------------------------------------------
def _settings_blob(col: Any) -> dict[str, Any]:
    blob = col.get_config(SMART_NOTES_KEY, default=None)
    return dict(blob) if isinstance(blob, dict) else {}


def _entry_for(blob: Mapping[str, Any], note_type: str) -> Optional[dict[str, Any]]:
    for entry in blob.get("note_types", []) or []:
        if isinstance(entry, dict) and entry.get("note_type") == note_type:
            return entry
    return None


def build_bundle(
    col: Any,
    note_type_name: str,
    *,
    tool_store: Any = None,
    include_note_type: bool = True,
    profile: str = "",
) -> NoteTypeBundle:
    """Read ``note_type_name``'s complete Smart Notes setup out of ``col``.

    Args:
        col: The open Anki collection.
        note_type_name: The note type to export.
        tool_store: A :class:`~omnia.plugins.smart_notes.engine.tools.user_tools.UserToolStore`
            to read ``user:`` tool sources from. ``None`` exports without them, which the
            import then reports as missing.
        include_note_type: Carry Anki's note type schema (fields/templates/css). Off exports
            configuration only — for refreshing a target whose templates must not be touched.
        profile: The Anki profile name, recorded for the import preview.

    Returns:
        The bundle.

    Raises:
        TransferError: No such note type, or it has no Smart Notes configuration.
    """
    model = col.models.by_name(note_type_name)
    if model is None:
        raise TransferError(
            f"This collection has no note type called {note_type_name!r}."
        )

    raw_entry = _entry_for(_settings_blob(col), note_type_name)
    if raw_entry is None:
        raise TransferError(
            f"{note_type_name!r} has no Smart Notes configuration here yet — there is nothing "
            "to export."
        )
    config = SmartNotesNoteTypeConfig(**raw_entry)

    deck_names: list[str] = []
    for deck_id in config.decks:
        try:
            name = col.decks.name(deck_id)
        except Exception:
            name = ""
        if name:
            deck_names.append(name)

    tools: dict[str, str] = {}
    if tool_store is not None:
        for tool_name in _referenced_user_tools(config):
            slug = tool_name[len(USER_TOOL_PREFIX) :]
            try:
                source = tool_store.read(slug)
            except Exception:
                source = None
            if source is not None:
                tools[tool_name] = source.render()

    return NoteTypeBundle(
        source=BundleSource(
            machine=platform.node(),
            platform=platform.platform(),
            profile=profile,
            anki_version=_anki_version(),
            exported_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
        note_type_name=note_type_name,
        anki_note_type=dict(model) if include_note_type else None,
        smart_notes=config,
        deck_names=deck_names,
        user_tools=tools,
    )


def _referenced_user_tools(config: SmartNotesNoteTypeConfig) -> list[str]:
    seen: list[str] = []
    for rule in config.fields:
        for spec in rule.tools:
            if spec.tool.startswith(USER_TOOL_PREFIX) and spec.tool not in seen:
                seen.append(spec.tool)
    return seen


def _anki_version() -> str:
    try:
        from anki.buildinfo import version

        return str(version)
    except Exception:
        return ""


# --- planning an import ---------------------------------------------------------------------
def plan_import(
    col: Any,
    bundle: NoteTypeBundle,
    *,
    mode: str = "",
    target_name: str = "",
    renames: Optional[Mapping[str, str]] = None,
    tool_store: Any = None,
) -> ImportPlan:
    """Work out what importing ``bundle`` into ``col`` would do, without doing any of it.

    Args:
        col: The open collection.
        bundle: The parsed bundle.
        mode: :data:`MODE_CREATE` / :data:`MODE_CLONE` / :data:`MODE_OVERWRITE`. Empty picks
            ``create`` when the name is free and ``overwrite`` when it is taken.
        target_name: The note type to write to (clone: the NEW name; overwrite: an existing
            one). Empty uses the bundle's own name.
        renames: ``{bundle field: target field}`` for an overwrite. Empty asks
            :func:`~omnia.plugins.smart_notes.transfer.remap.suggest_renames` for the
            unambiguous pairings and leaves the rest for the user.
        tool_store: Where ``user:`` tools would be written.

    Returns:
        The plan.

    Raises:
        TransferError: The requested mode cannot apply (e.g. cloning onto a name in use).
    """
    wanted = target_name or bundle.note_type_name
    existing = col.models.by_name(wanted)
    if not mode:
        mode = MODE_OVERWRITE if existing is not None else MODE_CREATE

    if mode == MODE_CREATE and existing is not None:
        raise TransferError(
            f"This collection already has a note type called {wanted!r}. Import it under a new "
            "name, or overwrite the existing one."
        )
    if mode == MODE_CLONE and existing is not None:
        raise TransferError(
            f"{wanted!r} is already taken — pick a name that is free for the copy."
        )
    if mode == MODE_OVERWRITE and existing is None:
        raise TransferError(
            f"There is no note type called {wanted!r} here to overwrite."
        )
    if mode in (MODE_CREATE, MODE_CLONE) and bundle.anki_note_type is None:
        raise TransferError(
            "This bundle carries configuration only, so it can only be applied to a note type "
            "that already exists here."
        )

    source_fields = bundle.field_names()
    if mode == MODE_OVERWRITE:
        target_fields = [
            str(f.get("name", "")) for f in (existing or {}).get("flds", [])
        ]
        mapping = (
            dict(renames) if renames else suggest_renames(source_fields, target_fields)
        )
    else:
        target_fields = source_fields
        mapping = {name: name for name in source_fields}

    plan = ImportPlan(
        mode=mode,
        target_note_type=wanted,
        renames=mapping,
        unmapped_source_fields=[n for n in source_fields if n not in mapping],
        unused_target_fields=[
            n for n in target_fields if n not in set(mapping.values())
        ],
        creates_note_type=mode in (MODE_CREATE, MODE_CLONE),
        replaces_config=_entry_for(_settings_blob(col), wanted) is not None,
        missing_tools=bundle.missing_user_tools(),
    )

    installed = set(tool_store.slugs()) if tool_store is not None else set()
    for tool_name in bundle.required_user_tools():
        slug = tool_name[len(USER_TOOL_PREFIX) :]
        if tool_name in bundle.user_tools and slug not in installed:
            plan.tools_to_install.append(tool_name)

    for name in bundle.deck_names:
        if col.decks.by_name(name) is None:
            plan.missing_decks.append(name)

    if plan.unmapped_source_fields:
        plan.warnings.append(
            "These configured fields have no counterpart here and their rules will be dropped: "
            + ", ".join(plan.unmapped_source_fields)
        )
    if plan.missing_tools:
        plan.warnings.append(
            "The bundle references user tools whose source it does not carry: "
            + ", ".join(plan.missing_tools)
        )
    if plan.missing_decks:
        plan.warnings.append(
            "These decks do not exist here, so the deck restriction will be relaxed to 'all "
            "decks': " + ", ".join(plan.missing_decks)
        )
    if plan.mode == MODE_OVERWRITE and plan.replaces_config:
        plan.warnings.append(
            f"{wanted!r} already has a Smart Notes configuration here; it will be replaced."
        )
    return plan


# --- applying an import ----------------------------------------------------------------------
def apply_bundle(
    col: Any,
    bundle: NoteTypeBundle,
    plan: ImportPlan,
    *,
    tool_store: Any = None,
) -> ImportResult:
    """Carry out ``plan``. Writes the note type, the tools, and the configuration.

    Order matters: tools first (so a chain never resolves to a tool that is not there yet),
    then the note type, then the configuration that refers to both.
    """
    written: list[str] = []
    if tool_store is not None:
        for tool_name in plan.tools_to_install:
            written.append(
                _write_tool(tool_store, tool_name, bundle.user_tools[tool_name])
            )

    created = False
    if plan.creates_note_type:
        _add_note_type(col, bundle, plan.target_note_type)
        created = True

    config, report = remap_note_type_config(
        bundle.smart_notes, plan.renames, note_type_name=plan.target_note_type
    )
    config = config.copy(update={"decks": _deck_ids(col, bundle.deck_names)})
    _write_config_entry(col, config)

    return ImportResult(
        note_type=plan.target_note_type,
        created_note_type=created,
        fields_configured=len(config.fields),
        tools_written=written,
        remap=report,
    )


def _write_tool(tool_store: Any, tool_name: str, source_text: str) -> str:
    from omnia.plugins.smart_notes.engine.tools.user_tools import UserToolSource

    slug = tool_name[len(USER_TOOL_PREFIX) :]
    tool_store.write(UserToolSource.parse(slug, source_text))
    return tool_name


def _add_note_type(col: Any, bundle: NoteTypeBundle, name: str) -> None:
    """Add the bundle's note type under ``name``.

    ``id = 0`` is what tells Anki to mint a fresh one: keeping the source collection's id would
    either collide with an unrelated local note type or silently graft this configuration onto
    it. ``usn``/``mod`` are ZEROED rather than removed for the same reason — they describe the
    OTHER collection's sync state, and Anki stamps its own on add — but the backend deserialises
    the legacy dict with both keys REQUIRED, so dropping them fails the add outright
    (``JsonError: missing field 'mod'``).
    """
    schema = dict(bundle.anki_note_type or {})
    schema["name"] = name
    schema["id"] = 0
    schema["usn"] = 0
    schema["mod"] = 0
    try:
        col.models.add_dict(schema)
    except Exception as exc:
        raise TransferError(f"Anki refused the note type: {exc}") from exc


def _deck_ids(col: Any, deck_names: list[str]) -> list[int]:
    ids: list[int] = []
    for name in deck_names:
        deck = col.decks.by_name(name)
        if deck is not None:
            ids.append(int(deck["id"]))
    return ids


def _write_config_entry(col: Any, config: SmartNotesNoteTypeConfig) -> None:
    """Replace (or append) this note type's entry, leaving every other one untouched.

    Reading, editing and writing back the WHOLE blob is what keeps the other note types' setups
    and the global flags intact — the config key holds all of them together.
    """
    blob = _settings_blob(col)
    entries = [e for e in (blob.get("note_types") or []) if isinstance(e, dict)]
    payload = config.dict()
    replaced = False
    for index, entry in enumerate(entries):
        if entry.get("note_type") == config.note_type:
            entries[index] = payload
            replaced = True
            break
    if not replaced:
        entries.append(payload)
    blob["note_types"] = entries
    col.set_config(SMART_NOTES_KEY, blob)
