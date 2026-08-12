"""Word Lookup settings (the plugin's own Pydantic v1 config).

The generic settings form is derived from this model via
:func:`omnia.core.config.schema.schema_from_model`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class WordLookupSettings(_Strict):
    """Settings for looking a word up in the collection from the desktop clipper."""

    note_types: list[str] = Field(
        default_factory=list,
        title="Searchable note types",
        description=(
            "Note type names the lookup searches (one per line).\n"
            "• Empty = search the WHOLE collection.\n"
            "• Listing the few note types you actually study makes hits precise and fast."
        ),
    )
    max_results: int = Field(
        5,
        ge=1,
        le=25,
        title="Max results",
        description="How many matching notes to return for one lookup.",
    )
    max_fields: int = Field(
        8,
        ge=1,
        le=30,
        title="Max fields per card",
        description=(
            "How many of a note's fields to show under the title.\n"
            "• Empty fields are always dropped first, so a 35-field note type stays readable.\n"
            "• Fields are kept in the note type's own field order (most important first)."
        ),
    )
    search_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        title="Fields to search, per note type",
        description=(
            "``{note type: [field, …]}``. A note type listed here is searched ONLY in those "
            "fields; anything not listed is searched across all of its fields.\n"
            "• A listed field matches the word as a WHOLE WORD: looking up 'port' finds "
            "'port' and 'port of call', but not 'important' or 'Portion'.\n"
            "• Narrowing to the headword field (e.g. Word) stops a hit on a word merely "
            "mentioned inside another card's examples or synonyms.\n"
            "• Matching is case-insensitive — Anki folds case itself, so LEVEL, Level and "
            "level are the same search."
        ),
    )
    display_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        title="Fields to show, per note type",
        description=(
            "``{note type: [field, …]}``. Listed fields are shown in the order given; a note "
            "type that is not listed falls back to the automatic pick (the first "
            "``max_fields`` non-empty fields, in the note type's own field order)."
        ),
    )
    hidden_fields: list[str] = Field(
        default_factory=list,
        title="Never show these fields",
        description=(
            "Field names to always hide in the lookup result (one per line, "
            "case-insensitive) — e.g. bookkeeping fields like 'Note ID'."
        ),
    )
    port: int = Field(
        8766,
        ge=1024,
        le=65535,
        title="Lookup service port",
        description=(
            "Loopback port the desktop clipper calls to run a lookup.\n"
            "• Bound to 127.0.0.1 ONLY — never reachable from the network.\n"
            "• Change it only if another program already uses this port."
        ),
    )
