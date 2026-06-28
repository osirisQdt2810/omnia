"""Smart-notes settings models (the plugin's own Pydantic v1 config).

Co-located with the plugin. Unlike the other features, smart_notes keeps a bespoke table
dialog for its UI (the per-note-type field table), so its ``config_model`` exists for typing
and validation rather than to drive the generic form — every top-level field here is either a
nested model or a complex list/dict the generic schema deriver skips anyway.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, validator


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


_GENERATION_TYPES = {"text", "image", "tts"}


class SmartNotesFieldRule(_Strict):
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
    target_field: str = ""
    kind: str = Field("text")  # text | image | tts
    prompt: str = ""
    deck_id: Optional[int] = None  # None = applies to all decks
    enabled: bool = True
    # Per-field provider overrides (empty = inherit the central [llm]/[tts] config).
    provider: str = ""
    model: str = ""
    voice: str = ""
    # Per-rule overwrite (the note-type config carries the real overwrite flag; the engine
    # threads it onto the compiled rule so skip logic can read it per field).
    overwrite: bool = False

    @validator("kind")
    def _validate_kind(cls, value: str) -> str:
        if value not in _GENERATION_TYPES:
            raise ValueError("kind must be 'text', 'image', or 'tts'")
        return value


class SmartNotesFieldConfig(_Strict):
    """The persisted generation config for ONE field on a note type.

    The base field of a note type is never represented here — it is the input. Every other
    field the user wants generated gets a row: its ``type`` (text/tts/image), a ``prompt``
    template that may reference the base field and other generated fields (``{{Word}}``,
    ``{{Meaning}}``), and optional per-field provider overrides (empty = inherit central
    ``[llm]``/``[tts]``). ``prompt_locked`` protects a hand-written prompt/type from being
    overwritten by the auto-smart generator. ``overwrite`` regenerates the field even when it
    already holds content.
    """

    field: str
    enabled: bool = False
    type: str = "text"  # text | image | tts
    prompt: str = ""
    prompt_locked: bool = False
    provider: str = ""
    model: str = ""
    voice: str = ""
    overwrite: bool = False

    @validator("type")
    def _validate_type(cls, value: str) -> str:
        if value not in _GENERATION_TYPES:
            raise ValueError("type must be 'text', 'image', or 'tts'")
        return value


class SmartNotesNoteTypeConfig(_Strict):
    """Per-note-type smart-notes config: one designated base field + per-field generation rows.

    ``base_field`` is the always-present input (e.g. "Word" — a single word OR a phrase) and is
    never generated. ``fields`` holds one :class:`SmartNotesFieldConfig` per other field the
    user configured. A field's prompt may reference the base field and other generated fields,
    forming a DAG resolved at generation time.
    """

    note_type: str
    base_field: str = ""
    fields: list[SmartNotesFieldConfig] = Field(default_factory=list)

    def generatable_fields(self) -> list[SmartNotesFieldConfig]:
        """Return the fields eligible for generation: enabled and not the base field."""
        return [
            field
            for field in self.fields
            if field.enabled and field.field != self.base_field
        ]


class SmartNotesSettings(_Strict):
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

    def note_type_config(self, note_type: str) -> Optional[SmartNotesNoteTypeConfig]:
        """Return the config for ``note_type``, or None when it has no smart-notes config."""
        for config in self.note_types:
            if config.note_type == note_type:
                return config
        return None
