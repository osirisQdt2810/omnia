"""Reading a bundle out of a collection, and applying one into it.

The only module in :mod:`~omnia.plugins.smart_notes.transfer` that touches Anki. It takes the
collection and the tool store as arguments rather than reaching for ``mw``, so the same code
runs under the GUI, under a script against a closed collection, and under a test with a fake.

Applying a bundle is deliberately a two-step: :func:`plan_import` decides what WOULD happen and
returns it for the user to look at, and :func:`apply_bundle` carries out a plan. An import can
rewrite prompts, drop rules and run tool code the file carried — none of which should first
become visible after the fact. Overwrite replaces the setups of the fields the mapping names
and leaves the target's other rules alone: the mapping table is the user's statement of what
to replace, and a rule it never mentions is work they never offered up.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Optional

from omnia.plugins.smart_notes.config import (
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine.tools.user_tools import USER_TOOL_PREFIX
from omnia.plugins.smart_notes.integration.store import SmartNotesStore
from omnia.plugins.smart_notes.transfer.bundle import (
    BundleSource,
    NoteTypeBundle,
)
from omnia.plugins.smart_notes.transfer.remap import (
    RemapReport,
    remap_note_type_config,
    suggest_renames,
)

#: The collection-config key the Smart Notes settings live under (ADR-006). Taken FROM the
#: store rather than restated: two spellings of one key is a rename waiting to go wrong.
SMART_NOTES_KEY = SmartNotesStore.KEY

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
    #: Target fields whose EXISTING rule the overwrite carries forward, because the file
    #: configures nothing for them. A subset of ``unused_target_fields``, which also holds
    #: fields that have no rule at all — including the base field.
    kept_local_fields: list[str] = dataclass_field(default_factory=list)
    creates_note_type: bool = False
    replaces_config: bool = False
    tools_to_install: list[str] = dataclass_field(default_factory=list)
    #: Tools the bundle carries that the user has NOT approved to run here. Installing one
    #: means executing its module body, so they are listed rather than run.
    unapproved_tools: list[str] = dataclass_field(default_factory=list)
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
    tools_failed: list[str] = dataclass_field(default_factory=list)
    remap: Optional[RemapReport] = None


# --- reading the collection ---------------------------------------------------------------
def _store(col: Any) -> SmartNotesStore:
    """The settings store, bound to THIS collection.

    Reusing it rather than reading ``col.get_config`` here is what keeps one owner of the key
    and one definition of the blob's shape — and means an import goes through the model that
    validates it instead of writing a raw dict past it.
    """
    return SmartNotesStore(col_provider=lambda: col)


def _settings(col: Any) -> SmartNotesSettings:
    return _store(col).load()


def build_bundle(
    col: Any,
    note_type_name: str,
    *,
    tool_store: Any = None,
    profile: str = "",
) -> NoteTypeBundle:
    """Read ``note_type_name``'s complete Smart Notes setup out of ``col``.

    Args:
        col: The open Anki collection.
        note_type_name: The note type to export.
        tool_store: A :class:`~omnia.plugins.smart_notes.engine.tools.user_tools.UserToolStore`
            to read ``user:`` tool sources from. ``None`` exports without them, which the
            import then reports as missing.
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

    config = _settings(col).note_type_config(note_type_name)
    if config is None:
        raise TransferError(
            f"{note_type_name!r} has no Smart Notes configuration here yet — there is nothing "
            "to export."
        )

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
        anki_note_type=dict(model),
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
    tool_loader: Any = None,
    approved_tools: Optional[Iterable[str]] = None,
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
        tool_loader: The user-tool loader (its ``store`` says what is already installed).
        approved_tools: The ``user:`` tool names the user has read and agreed to run here.
            Installing a carried tool EXECUTES its module body, so the default (``None``)
            approves nothing and every carried tool is listed for review instead. A caller
            that has obtained consent passes the names it obtained it for.

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
        # ``is None``, not truthiness: setting every row to "not imported" sends an EMPTY
        # mapping, and that is a decision. Falling back to the suggestion there would import
        # everything the user had just declined.
        mapping = (
            dict(renames)
            if renames is not None
            else suggest_renames(source_fields, target_fields)
        )
    else:
        target_fields = source_fields
        mapping = {name: name for name in source_fields}

    # The page is the UNTRUSTED side of a pycmd boundary, so both of its guarantees about the
    # mapping are re-checked here: that no two source fields collapse onto one target, and
    # that every target is a field the note type actually has. Its <select> constrains the
    # second today, which is exactly why leaving it unchecked would go unnoticed if it ever
    # stopped doing so.
    unknown = sorted({t for t in mapping.values() if t and t not in set(target_fields)})
    if unknown and mode == MODE_OVERWRITE:
        raise TransferError(
            "These are not fields of " f"{wanted!r}: " + ", ".join(unknown)
        )
    # Two source fields onto one target would write two rules with the same ``field``.
    collapsed = [
        target
        for target in {t for t in mapping.values() if t}
        if sum(1 for value in mapping.values() if value == target) > 1
    ]
    if collapsed:
        raise TransferError(
            "These fields would both map onto the same field here, which cannot work: "
            + ", ".join(sorted(collapsed))
        )

    local = _settings(col).note_type_config(wanted)
    incoming_fields = {mapping.get(rule.field) for rule in bundle.smart_notes.fields}
    plan = ImportPlan(
        mode=mode,
        target_note_type=wanted,
        renames=mapping,
        unmapped_source_fields=[n for n in source_fields if n not in mapping],
        unused_target_fields=[
            n for n in target_fields if n not in set(mapping.values())
        ],
        # Only the fields whose rules the merge actually carries forward. The wider
        # "receives nothing from the file" set includes fields with no rule at all — saying
        # their rules are kept is noise at best and, for the base field, wrong.
        kept_local_fields=(
            [rule.field for rule in local.fields if rule.field not in incoming_fields]
            if local is not None and mode == MODE_OVERWRITE
            else []
        ),
        creates_note_type=mode in (MODE_CREATE, MODE_CLONE),
        replaces_config=local is not None,
        missing_tools=bundle.missing_user_tools(),
    )

    store = getattr(tool_loader, "store", None)
    installed = set(store.slugs()) if store is not None else set()
    approved = set(approved_tools or ())
    for tool_name in bundle.required_user_tools():
        slug = tool_name[len(USER_TOOL_PREFIX) :]
        if tool_name not in bundle.user_tools or slug in installed:
            continue
        # Installing a carried tool runs it. The add-on's safety boundary for user tools is
        # the read-and-run review (see ``risky_operations``), not the import allowlist — which
        # had to permit ``os`` and ``subprocess`` — so a bundle from someone else must not be
        # able to execute anything the reader has not looked at.
        if tool_name in approved:
            plan.tools_to_install.append(tool_name)
        else:
            plan.unapproved_tools.append(tool_name)

    for name in bundle.deck_names:
        if col.decks.by_name(name) is None:
            plan.missing_decks.append(name)

    if plan.unmapped_source_fields:
        plan.warnings.append(
            "These configured fields have no counterpart here and their rules will be dropped: "
            + ", ".join(plan.unmapped_source_fields)
        )
    if plan.kept_local_fields:
        plan.warnings.append(
            "These fields are not configured by the file, so the rules they already have "
            "here are kept as they are: " + ", ".join(plan.kept_local_fields)
        )
    if plan.mode == MODE_OVERWRITE and local is not None:
        plan.warnings.extend(_envelope_warnings(col, bundle, mapping, local))
    if plan.missing_tools:
        plan.warnings.append(
            "The bundle references user tools whose source it does not carry: "
            + ", ".join(plan.missing_tools)
        )
    if plan.unapproved_tools:
        plan.warnings.append(
            "These user tools travel with the file but will NOT be installed until you "
            "have read them and said so — installing one runs it: "
            + ", ".join(plan.unapproved_tools)
        )
    if plan.missing_decks:
        fallback = (
            "this note type keeps the deck restriction it already has"
            if plan.mode == MODE_OVERWRITE and local is not None and local.decks
            else "the configuration applies to all decks"
        )
        plan.warnings.append(
            "These decks do not exist here and will be left out of the deck restriction "
            f"(if none of them resolve, {fallback}): " + ", ".join(plan.missing_decks)
        )
    if plan.mode == MODE_OVERWRITE and plan.replaces_config:
        plan.warnings.append(
            f"{wanted!r} already has a Smart Notes configuration here. The fields you map "
            "below have their rules replaced by the file's; the rest keep theirs."
        )
    return plan


# --- applying an import ----------------------------------------------------------------------
def apply_bundle(
    col: Any,
    bundle: NoteTypeBundle,
    plan: ImportPlan,
    *,
    tool_loader: Any = None,
) -> ImportResult:
    """Carry out ``plan``. Writes the tools, the note type, and the configuration.

    Order matters, and "tools first" means WRITTEN AND LOADED first. Writing the file is not
    enough: a chain resolves a tool through the registry, and :func:`remap_note_type_config`
    asks the registry which of a tool's params name fields. A tool that is on disk but
    unregistered is invisible to both — so its ``sentence_field`` keeps the OLD name while
    ``depends_on`` is rewritten to the new one, leaving the graph and the tool disagreeing,
    and the tool reading a field the note type does not have. It is also dead for the rest of
    the session, since nothing loads ``user_files/tools`` again until the next Anki start.
    """
    written: list[str] = []
    failed: list[str] = []
    if tool_loader is not None:
        for tool_name in plan.tools_to_install:
            outcome = _install_tool(
                tool_loader, tool_name, bundle.user_tools[tool_name]
            )
            (written if outcome is None else failed).append(
                tool_name if outcome is None else f"{tool_name}: {outcome}"
            )

    created = False
    if plan.creates_note_type:
        _add_note_type(col, bundle, plan.target_note_type)
        created = True

    config, report = remap_note_type_config(
        bundle.smart_notes, plan.renames, note_type_name=plan.target_note_type
    )
    config = config.copy(update={"decks": _deck_ids(col, bundle.deck_names)})
    if plan.mode == MODE_OVERWRITE:
        config = _merged_with_local(col, config, plan.target_note_type)
    _write_config_entry(col, config)

    return ImportResult(
        note_type=plan.target_note_type,
        created_note_type=created,
        fields_configured=len(config.fields),
        tools_written=written,
        tools_failed=failed,
        remap=report,
    )


def _envelope_warnings(
    col: Any,
    bundle: NoteTypeBundle,
    mapping: Mapping[str, str],
    local: SmartNotesNoteTypeConfig,
) -> list[str]:
    """Say what an overwrite does to the base field and the deck scope.

    Neither is a rule, so neither shows up in the mapping table the user is reading — and both
    change what generates. See :func:`_merged_with_local` for what actually happens to them.
    """
    warnings: list[str] = []
    incoming_base = bundle.smart_notes.base_field
    mapped_base = mapping.get(incoming_base, "") if incoming_base else ""
    if incoming_base and not mapped_base and local.base_field:
        warnings.append(
            f"The file's base field ({incoming_base!r}) has no counterpart here, so "
            f"{local.base_field!r} stays the base field."
        )
    elif mapped_base and local.base_field and mapped_base != local.base_field:
        warnings.append(
            f"The base field changes from {local.base_field!r} to {mapped_base!r} — every "
            "rule with no prompt of its own generates from it."
        )

    if local.decks and not _deck_ids(col, bundle.deck_names):
        names = [name for name in (col.decks.name(i) for i in local.decks) if name]
        warnings.append(
            "The file carries no deck restriction that applies here, so this note type keeps "
            "its own" + (f" ({', '.join(names)})." if names else ".")
        )
    return warnings


def _merged_with_local(
    col: Any, config: SmartNotesNoteTypeConfig, note_type: str
) -> SmartNotesNoteTypeConfig:
    """Return ``config`` with everything the imported file did not speak to left as it was.

    Overwrite means "put the file's setup onto this note type" — not "delete the parts the
    file has nothing to say about". That has to cover the WHOLE config, not just the rules:
    the first version of this merged ``fields`` alone, so a bundle whose base field had no
    counterpart here cleared the local one, and every kept rule with an empty prompt then
    compiled with ``source_field=""`` and generated from nothing. Kept but inert is worse than
    dropped, because nothing says it happened.

    Per key, and each for its own reason:

    ``fields``
        The file's rules, then the local rules for fields it does not configure, in the order
        the target had them. Their edges stay valid — overwrite does not touch the note type's
        fields, so everything they name still exists.
    ``base_field``
        The file's, unless the mapping gave it no counterpart here (then it is ``""``), in
        which case the local one stays. A config with no base field generates nothing.
    ``decks``
        The file's, unless it names none that resolve here. ``[]`` does not mean "no
        restriction was carried", it means **all decks** — so taking the file's empty list
        would silently switch generation on in decks the user had deliberately excluded.
    ``node_positions``
        Both, the file's winning. They are per field name, so the kept rules keep their place
        on the graph canvas instead of the layout jumping.
    anything else
        Local values survive when the file has no key for them, which is what keeps a config
        written by a NEWER Omnia intact through an import from an older one (ADR-010).

    Args:
        col: The collection to read the target's current configuration from.
        config: The remapped configuration about to be written.
        note_type: The target note type's name.

    Returns:
        ``config``, or the merge of it over what is already there.
    """
    existing = _settings(col).note_type_config(note_type)
    if existing is None:
        return config
    imported = {rule.field for rule in config.fields}
    merged = {**existing.dict(), **config.dict()}
    merged.update(
        fields=[
            *config.fields,
            *[rule for rule in existing.fields if rule.field not in imported],
        ],
        base_field=config.base_field or existing.base_field,
        decks=config.decks or existing.decks,
        node_positions={**existing.node_positions, **config.node_positions},
    )
    return SmartNotesNoteTypeConfig(**merged)


def _install_tool(tool_loader: Any, tool_name: str, source_text: str) -> Optional[str]:
    """Write a carried tool and register it. Returns None on success, else the reason.

    The write-then-load pair mirrors what the Tools tab does when the user saves one
    (``UserToolsController.on_save``): the store owns the file, the loader owns the registry,
    and only the second makes the tool resolvable.
    """
    from omnia.plugins.smart_notes.engine.tools.user_tools import UserToolSource

    slug = tool_name[len(USER_TOOL_PREFIX) :]
    try:
        tool_loader.store.write(UserToolSource.parse(slug, source_text))
    except Exception as exc:
        return f"could not be saved ({exc})"
    load = tool_loader.load(slug)
    if not getattr(load, "ok", False):
        # The import still applies — but the chain using this tool will not run, and saying so
        # is the difference between a known gap and a mystery.
        return getattr(load, "error", "") or "could not be loaded"
    return None


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

    Loading, editing and saving the WHOLE settings object is what keeps the other note types'
    setups and the global flags intact — the config key holds all of them together, and
    ``PersistedModel``'s ``extra="allow"`` carries through anything a newer Omnia added.
    """
    settings = _settings(col)
    entries = list(settings.note_types)
    for index, entry in enumerate(entries):
        if entry.note_type == config.note_type:
            entries[index] = config
            break
    else:
        entries.append(config)
    _store(col).save(settings.copy(update={"note_types": entries}))
