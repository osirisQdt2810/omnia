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

from typing import Optional

from pydantic import Field, validator

from omnia.core.config.base import PersistedModel, StrictModel

_GENERATION_TYPES = {"text", "image", "tts"}


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

    def note_type_config(self, note_type: str) -> Optional[SmartNotesNoteTypeConfig]:
        """Return the config for ``note_type``, or None when it has no smart-notes config."""
        for config in self.note_types:
            if config.note_type == note_type:
                return config
        return None

    def integration_autogen_enabled(self, key: str) -> bool:
        """Return whether auto-generation is enabled for integration ``key`` (default off)."""
        return bool(self.auto_generate_integrations.get(key, False))
