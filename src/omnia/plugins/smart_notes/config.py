"""Smart-notes settings models (the plugin's own Pydantic v1 config).

Co-located with the plugin. Unlike the other features, smart_notes keeps a bespoke table
dialog for its UI (the per-note-type field table), so its ``config_model`` exists for typing
and validation rather than to drive the generic form — every top-level field here is either a
nested model or a complex list/dict the generic schema deriver skips anyway.

The whole settings tree is persisted in the SYNCED collection config (``omnia:smart_notes``),
so it is loaded — and written back — by every device the user syncs, on whatever Omnia version
each one runs. Every model in that tree therefore extends
:class:`~omnia.core.config.base.PersistedModel` (unknown keys survive a round trip instead of
raising); only :class:`SmartNotesFieldRule`, which the engine compiles in memory and never
stores, stays a :class:`~omnia.core.config.base.StrictModel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import Field, validator

from omnia import envs
from omnia.core.config.base import PersistedModel, StrictModel

_GENERATION_TYPES = {"text", "image", "tts"}

# The tool a field with no explicit chain runs: the provider-backed path smart_notes had
# before tool chains existed. Named here rather than on the tool class so the config layer
# never has to import the engine (the dependency runs engine → config).
DEFAULT_TOOL_NAME = "ai"

# Top-level SmartNotesSettings keys added AFTER the blob's shape was already syncing, and
# therefore omitted from the serialized form while they hold their default (see
# SmartNotesSettings.dict). Listed once so the prune cannot drift from the set it covers.
_PRUNE_WHILE_DEFAULT = ("max_concurrent_generations", "batch_notes_per_call")

# The ceilings this build will honour, applied by SmartNotesSettings.workers() /
# .notes_per_call() rather than by a model validator — see max_concurrent_generations for why a
# SYNCED field must not carry a range. They live next to the fields they bound so the settings
# model, the GUI controller and the runner cannot drift apart on what "too big" means.
MAX_WORKERS = 16
MAX_NOTES_PER_CALL = 20


class FieldDep(PersistedModel):
    """An explicit dependency edge from one field onto a prerequisite ``field``.

    ``kind`` carries the semantics: a ``"hard"`` dependency both orders generation AND
    blocks (the dependent is skipped when the prerequisite is empty/failed); a ``"soft"``
    dependency orders only and never blocks. An explicit entry for an edge already derived
    from a prompt ``{{ref}}`` overrides that edge's (default ``"hard"``) kind. The model layer
    intentionally does NOT validate self/unknown references — a field may legitimately depend
    on a not-yet-created field; whole-note-type checks live in the engine.

    ``auto`` is provenance metadata only: ``False`` (the back-compat default) marks a
    user/explicit edge, ``True`` marks one written by the dependency classifier. It does NOT
    affect the edge's kind — the graph and engine read only ``field``/``kind`` — but the
    prompt↔graph reconciler uses it to decide which stale edges it may safely drop (a vanished
    auto edge is cleaned; a user edge is preserved).

    A ``kind`` this version does not know (a future release's third semantics) is KEPT
    VERBATIM rather than rejected or rewritten: every consumer asks "is this edge ``hard``?"
    (``graph.py``'s adjacency + blocking filters), so an unrecognised kind already degrades to
    the weaker "soft" behaviour — it orders generation but can never block it. Keeping the raw
    string means an older device hands the edge back to the newer one unchanged. The GUI
    payload path normalises the kind before it ever builds one of these
    (``gui/smart_notes/html.py``), so a typo cannot arrive from the dialog.
    """

    field: str
    kind: str = "hard"
    auto: bool = False


class FieldToolConfig(PersistedModel):
    """One entry of a field's ordered tool chain, as persisted on the note-type row.

    ``tool`` is a registered tool name (``"ai"``, ``"cloze"``, ``"user:<slug>"``); ``params``
    are that tool's per-field options, validated against the tool's own params model when it
    runs. A name this device does not have (a user tool authored on another machine) is kept
    verbatim and simply skipped at run time, so the chain degrades instead of breaking.
    """

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolUsage:
    """One place a tool name appears in the settings: ``field`` of ``note_type``'s chain.

    A plain value object rather than a model — it is never persisted, it is computed on demand
    by :meth:`SmartNotesSettings.fields_using_tool` so the Tools tab can tell the user exactly
    what a delete would leave behind ("Vocab · Audio Ext").
    """

    note_type: str
    field: str

    def __str__(self) -> str:
        """Render the usage the way the UI shows it (``"Vocab · Audio Ext"``)."""
        return f"{self.note_type} · {self.field}"


class CompiledToolSpec(StrictModel):
    """One entry of the tool chain the ENGINE runs (compiled from config, never persisted)."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)


def default_tool_chain() -> tuple[CompiledToolSpec, ...]:
    """Return the legacy chain: the single ``ai`` tool, with no params.

    The one place that knows what "no tools configured" means. Used both as
    :attr:`SmartNotesFieldRule.tools`' default (so a rule built directly — the preview and
    custom-prompt palettes — behaves exactly as it did before tools existed) and by
    :func:`~omnia.plugins.smart_notes.engine.rules.compile_field_rule` for an empty
    :attr:`SmartNotesFieldConfig.tools` list.
    """
    return (CompiledToolSpec(name=DEFAULT_TOOL_NAME),)


class SmartNotesFieldRule(StrictModel):
    """A single, self-contained generation rule (the per-call shape the engine consumes).

    This is the unit :meth:`GenerationService.generate` operates on: read ``source_field``,
    write ``target_field`` via ``kind``. It is NOT the persisted note-type config (see
    :class:`SmartNotesFieldConfig`) — the engine compiles a note type's enabled fields into
    these rules at generation time, and the one-off custom-prompt palette builds one directly.

    Provider selection stays central (``[llm]`` / ``[tts]`` + the ProviderHub); the
    ``provider``/``model``/``voice`` fields here are optional per-rule OVERRIDES that layer
    on top — empty means "inherit the active central provider".

    Every field has a default, so partial dicts still build.
    """

    note_type: str = ""
    # The note type's base (input) field, threaded on by the compiler. A tool whose params
    # default to "the base field" (``cloze``'s ``word_field``) needs it, and the rule is the
    # only thing a tool is handed. Empty for a rule built without a note-type config.
    base_field: str = ""
    source_field: str = ""
    # True when ``source_field`` is purely the empty-prompt → base-field fallback (used so a
    # promptless field can still read the base at generation time). Such a fallback source is
    # NOT a derived dependency: the graph / ordering / blocking ignore it (the base is always
    # present), while generation still reads ``source_field`` as before.
    source_is_base_fallback: bool = False
    target_field: str = ""
    kind: str = Field("text")  # text | image | tts
    prompt: str = ""
    deck_id: Optional[int] = None  # None = applies to all decks
    enabled: bool = True
    # Per-field provider overrides (empty = inherit the central [llm]/[tts] config).
    provider: str = ""
    model: str = ""
    voice: str = ""
    # TTS language code (e.g. "vi"); empty = auto-detect the spoken text's language.
    language: str = ""
    # Per-rule overwrite (the note-type config carries the real overwrite flag; the engine
    # threads it onto the compiled rule so skip logic can read it per field).
    overwrite: bool = False
    # Explicit dependency edges threaded from the field config (union with derived {{refs}});
    # ordering + blocking read these alongside the derived edges.
    depends_on: list[FieldDep] = Field(default_factory=list)
    # The ordered tool chain the pipeline runs for this field: the first tool that produces a
    # result wins. Defaults to the legacy single-``ai`` chain, so a rule constructed directly
    # (preview / custom-prompt / auto-smart) generates exactly as it did before tools existed.
    tools: tuple[CompiledToolSpec, ...] = Field(default_factory=default_tool_chain)

    @validator("kind")
    def _validate_kind(cls, value: str) -> str:
        if value not in _GENERATION_TYPES:
            raise ValueError("kind must be 'text', 'image', or 'tts'")
        return value


class SmartNotesFieldConfig(PersistedModel):
    """The persisted generation config for ONE field on a note type.

    The base field of a note type is never represented here — it is the input. Every other
    field the user wants generated gets a row: its ``type`` (text/tts/image), a ``prompt``
    template that may reference the base field and other generated fields (``{{Word}}``,
    ``{{Meaning}}``), and optional per-field provider overrides (empty = inherit central
    ``[llm]``/``[tts]``). ``prompt_locked`` protects a hand-written prompt/type from being
    overwritten by the auto-smart generator. ``overwrite`` regenerates the field even when it
    already holds content.

    A ``type`` this version does not implement (a generation kind a NEWER Omnia added) is KEPT
    VERBATIM, exactly like :attr:`FieldDep.kind`: the blob syncs, so rewriting the value here
    would make an older device write the damage back for a note type its user never touched
    (``store.save`` persists the WHOLE ``settings.dict()``), destroying the row's semantics on
    the authoring device. The row is neutralised where it is CONSUMED instead — see
    :meth:`supports_generation` — so it loads, round-trips and simply never generates.
    """

    field: str
    enabled: bool = False
    type: str = "text"  # text | image | tts
    prompt: str = ""
    prompt_locked: bool = False
    provider: str = ""
    model: str = ""
    voice: str = ""
    # TTS language code (e.g. "vi"); empty = auto-detect the spoken text's language.
    language: str = ""
    overwrite: bool = False
    # Explicit dependency edges onto prerequisite fields (union with derived {{refs}}); a
    # "hard" dep both orders and blocks, a "soft" dep orders only. An explicit entry overrides
    # the kind of a derived edge for the same prerequisite.
    depends_on: list[FieldDep] = Field(default_factory=list)
    # Ordered tool chain for this field, tried until one produces a result. EMPTY (the
    # default, and what every blob written before tool chains existed carries) means the
    # legacy single-``ai`` chain — see :func:`default_tool_chain`. Never SERIALIZED while it is
    # empty — see :meth:`dict`.
    tools: list[FieldToolConfig] = Field(default_factory=list)

    def dict(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize the row, OMITTING ``tools`` while the chain is empty.

        An empty chain carries no information — it IS the legacy default — so writing the key
        would only announce this release's schema to the synced collection. That matters
        because the blob syncs to devices on OTHER releases: one on a build from before
        :class:`~omnia.core.config.base.PersistedModel` (ADR-010) still validates with
        ``extra = "forbid"`` and has no ``try`` around
        :meth:`~omnia.plugins.smart_notes.integration.store.SmartNotesStore.load`, so a single
        unknown key there is not a lost setting but a crash on every note-add hook. Omitting it
        keeps the persisted blob byte-identical to a pre-tools one for every legacy config,
        and a field the user actually configures a chain on is the first thing that ever
        writes the key.

        Pruning lives HERE rather than in the store because the model owns the meaning of "no
        chain": the store persists ``settings.dict()`` wholesale and would have to walk the
        note-type tree looking for a key two levels down, and every other caller that
        serializes a settings tree would still leak it. Pydantic v1 serializes a NESTED model
        through that model's own ``dict()``, so this one override covers the whole tree.

        Args:
            **kwargs: Passed through to :meth:`pydantic.BaseModel.dict` unchanged.

        Returns:
            The row's serialized form, without a ``tools`` key when the chain is empty.
        """
        data: dict[str, Any] = super().dict(**kwargs)
        if not data.get("tools"):
            data.pop("tools", None)
        return data

    def supports_generation(self) -> bool:
        """Whether THIS version implements the row's ``type`` (and may therefore generate it).

        The single guard for an unknown generation type, asked by every consumer that would
        otherwise act on it: :meth:`SmartNotesNoteTypeConfig.generatable_fields` (so the row is
        never generated) and the rule compiler (so the strict
        :class:`SmartNotesFieldRule` it builds gets a ``kind`` it can validate). Neutralising
        on read rather than on load is what lets the raw ``type`` survive the round trip back
        to the newer device that wrote it.

        Returns:
            True when ``type`` is a generation type this release implements.
        """
        return self.type in _GENERATION_TYPES


class SmartNotesNoteTypeConfig(PersistedModel):
    """Per-note-type smart-notes config: one designated base field + per-field generation rows.

    ``base_field`` is the always-present input (e.g. "Word" — a single word OR a phrase) and is
    never generated. ``fields`` holds one :class:`SmartNotesFieldConfig` per other field the
    user configured. A field's prompt may reference the base field and other generated fields,
    forming a DAG resolved at generation time. ``decks`` scopes this config to a subset of decks
    (by deck id); an empty list means it applies to ALL decks.
    """

    note_type: str
    base_field: str = ""
    fields: list[SmartNotesFieldConfig] = Field(default_factory=list)
    decks: list[int] = Field(
        default_factory=list
    )  # deck ids this config applies to; [] = all decks
    # User-pinned node positions for the dependency-graph canvas: a field name (original
    # case, incl. the base field which has no row) -> its [x, y] top-left pixel coords. An
    # entry overrides the auto-computed flow layout so a moved node survives tab switch + save.
    node_positions: dict[str, list[float]] = Field(default_factory=dict)

    def generatable_fields(self) -> list[SmartNotesFieldConfig]:
        """Return the fields eligible for generation: enabled, not the base, type supported.

        A row whose ``type`` only a NEWER Omnia implements is skipped here rather than
        rewritten on load (see :meth:`SmartNotesFieldConfig.supports_generation`), so this
        version never generates the WRONG content into a field the newer version means to fill
        differently — while the row itself syncs back untouched.
        """
        return [
            field
            for field in self.fields
            if field.enabled
            and field.field != self.base_field
            and field.supports_generation()
        ]


class SmartNotesSettings(PersistedModel):
    """smart_notes feature settings, organised PER NOTE TYPE (provider config is shared).

    Each :class:`SmartNotesNoteTypeConfig` designates one base (input) field and configures
    how every other field is generated. A fresh, empty config (no ``note_types``) validates,
    so smart_notes ships disabled with no rules and never crashes on load.
    """

    note_types: list[SmartNotesNoteTypeConfig] = Field(default_factory=list)
    # Skip a field whose referenced source fields are ALL blank unless this is True.
    allow_empty_fields: bool = False
    # Whether automatic batch generation regenerates fields it already filled.
    regenerate_when_batching: bool = True
    # Pre-generate a card's empty smart fields ahead of the reviewer (best-effort).
    generate_at_review: bool = False
    # Per-integration auto-generate toggles (integration key -> enabled). Empty ⇒ every
    # integration OFF, so no external source triggers LLM spend until the user opts in.
    auto_generate_integrations: dict[str, bool] = Field(default_factory=dict)
    # Discard a clipped note when auto-generation filled NOTHING — the card would hold only the
    # captured word, which is not worth reviewing. Applies ONLY to notes arriving from an
    # integration (the clippers), and only when generation actually ran and produced nothing:
    # a note type with no smart-notes config never reaches this, and a note whose generation
    # RAISED is kept so a provider hiccup cannot throw a capture away.
    discard_unfilled_clips: bool = True
    # How many generation units (one field's tool chain) may run at once, and — via the
    # limiter, which ``pooled_dispatch`` narrows to exactly this for the duration of a run — how
    # hard the PROVIDER is hit. Exactly this, not "this + 1": a reserved lane for an interactive
    # call was tried, could never be used (Anki serialises every collection QueryOp) and made
    # the bound provably non-binding. ``OMNIA_MAX_CONCURRENT_REQUESTS`` is what sets the two
    # apart when they should differ. Lives at the top
    # level, not per note type: the quota belongs to the provider account, which every note
    # type shares, so a per-note-type bound would let two note types each open N connections
    # against the same account. 1 = fully sequential, exactly as before concurrency existed.
    #
    # Deliberately NO ``ge``/``le`` here; the bound is applied where the value is USED instead.
    # This blob syncs and :meth:`SmartNotesStore.load` has no ``try`` around ``parse_obj``, so
    # a range validator would turn a value written by a future release that raised the ceiling
    # into a ValidationError — which PluginManager swallows into "the feature silently never
    # enables" (ADR-010). Clamping on load is no better: it would rewrite the other device's
    # value on the next save. The stored number round-trips untouched, and this build simply
    # runs as many workers as it is willing to.
    #
    # DEFAULT 1 — a user who changed nothing gets exactly the pre-concurrency behaviour: no
    # pool is created, one field at a time, one provider request in flight. Concurrency raises
    # the load on a shared provider ACCOUNT, and a default that raises it for someone who never
    # asked is a rate-limit bill they did not opt into.
    max_concurrent_generations: int = 1
    # How many NOTES one provider request may cover for the same field. Every note of a note
    # type shares that field's prompt template, so K of them can be asked in one call — see
    # ADR-017 and :mod:`~omnia.plugins.smart_notes.engine.batching`.
    #
    # 1 means OFF for this collection. The DEFAULT is 10, matching
    # ``envs.OMNIA_SMART_NOTES_BATCHING``: batching cuts requests by about two thirds and the
    # shipped worker count is 1, where a chunk has no parallelism to destroy. ONE key rather
    # than a bool plus an int — "off" is exactly what K = 1 expresses, and every extra persisted
    # key is another ADR-010 surface.
    #
    # No ``ge``/``le``, and clamped at the point of use, for the same reason as the field above.
    #
    # This is what the user asks for; ``envs.OMNIA_SMART_NOTES_BATCHING`` is what the machine
    # allows, and :meth:`notes_per_call` — the only place either is read — takes the smaller.
    batch_notes_per_call: int = 10

    def workers(self) -> int:
        """How many generation units may run at once, clamped to what this build supports.

        Clamped HERE, at the single read site, rather than on the model: the blob syncs, so a
        value written by a release that raised the ceiling has to degrade rather than raise
        (ADR-010). Every path that starts a pool goes through this one method, so the editor
        button and review-time pre-generation cannot fan out wider than the batch runner —
        they used to read the raw field and were bounded by nothing.
        """
        return max(1, min(int(self.max_concurrent_generations), MAX_WORKERS))

    def notes_per_call(self) -> int:
        """The EFFECTIVE K for LAYER 3 batching — the env knob's K, or less if the user asked.

        Two inputs, one rule: ``envs.OMNIA_SMART_NOTES_BATCHING`` decides, and the synced
        ``batch_notes_per_call`` may only ask for a SMALLER chunk than the environment allows.
        The env knob is therefore both the off switch (``-1``, or anything below 1) and the
        ceiling, so a machine can always force a collection's batching down — including to off —
        without editing a setting that would then sync to every other device.

        A returned 1 does not mean "batching at a width of one": ``batch_planner`` hands back
        ``SOLO_PLANNER``, so the code that builds envelopes, mints item ids and parses batched
        replies is never constructed and every field takes the pre-LAYER-3 path. The stored
        number is left untouched either way, so raising the ceiling again restores whatever this
        user (or another device) chose.
        """
        allowed = int(envs.OMNIA_SMART_NOTES_BATCHING)
        if allowed < 1:
            return 1
        return max(1, min(int(self.batch_notes_per_call), allowed, MAX_NOTES_PER_CALL))

    def dict(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize the settings, OMITTING the performance keys while they are default.

        Same reasoning as :meth:`SmartNotesFieldConfig.dict`, and the same stakes: this blob
        SYNCS. A device on a build from before
        :class:`~omnia.core.config.base.PersistedModel` (ADR-010) validates it with
        ``extra = "forbid"`` and has no ``try`` around
        :meth:`~omnia.plugins.smart_notes.integration.store.SmartNotesStore.load`, so an
        unknown key there is not a lost setting — it is a crash on every note-add hook. A user
        who never opens Advanced therefore keeps writing a blob byte-identical to today's, and
        a key appears only once someone actually changes it.

        Args:
            **kwargs: Passed through to :meth:`pydantic.BaseModel.dict` unchanged.

        Returns:
            The settings' serialized form, without the still-default performance keys.
        """
        data: dict[str, Any] = super().dict(**kwargs)
        for key in _PRUNE_WHILE_DEFAULT:
            if data.get(key) == type(self).__fields__[key].default:
                data.pop(key, None)
        return data

    def note_type_config(self, note_type: str) -> Optional[SmartNotesNoteTypeConfig]:
        """Return the config for ``note_type``, or None when it has no smart-notes config."""
        for config in self.note_types:
            if config.note_type == note_type:
                return config
        return None

    def integration_autogen_enabled(self, key: str) -> bool:
        """Return whether auto-generation is enabled for integration ``key`` (default off)."""
        return bool(self.auto_generate_integrations.get(key, False))

    def fields_using_tool(self, tool: str) -> list[ToolUsage]:
        """Return every field whose tool chain names ``tool``, across all note types.

        Asked before deleting a user-authored tool, so the confirmation can NAME the fields that
        will change behaviour instead of warning in the abstract. Nothing breaks if the user
        deletes it anyway — the pipeline degrades an unresolvable name to ``unknown_tool`` and
        tries the next tool in the chain — but "which cards does this affect?" is a question
        only the settings can answer.

        Args:
            tool: The registered tool name (``"user:extract-ext"``).

        Returns:
            One :class:`ToolUsage` per referencing field, in note-type then field order.
        """
        usages: list[ToolUsage] = []
        for note_type in self.note_types:
            for field in note_type.fields:
                if any(entry.tool == tool for entry in field.tools):
                    usages.append(ToolUsage(note_type.note_type, field.field))
        return usages
