"""The on-disk format for moving ONE note type's Smart Notes setup between collections.

A Smart Notes configuration is not self-contained. Three of the things it depends on do not
travel with the collection config, and a "bundle" that carried only the config would import
cleanly and then fail to generate:

* **the note type itself** — the target machine may not have it at all, and the config is keyed
  by note type NAME with per-field rules that mean nothing without those fields;
* **user-authored tools** — a ``user:`` tool is a real ``.py`` file under ``user_files/tools/``
  and is deliberately NOT synced (approving code on one device must never execute it on every
  other), so a chain referencing one arrives pointing at a tool that does not exist here;
* **decks** — ``decks`` holds Anki deck IDs, which are per-collection integers. The same id on
  the other machine is a different deck, or no deck.

So a bundle carries the note type schema, the config, the tool sources, and deck NAMES. It is
plain JSON: reviewable before import, diffable, and safe to send through any channel.

Pure logic — no ``aqt``/``anki`` — so the format unit-tests headless; reading a collection to
build one, and writing one into a collection, live in :mod:`.collection`.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.smart_notes.config import SmartNotesNoteTypeConfig
from omnia.plugins.smart_notes.engine.tools.user_tools import USER_TOOL_PREFIX

#: Bumped when a field is added that an older Omnia could not simply ignore. Import refuses a
#: HIGHER version rather than guessing: a bundle from a newer Omnia may encode a chain shape
#: this one would silently drop, and a half-understood import is worse than a clear refusal.
BUNDLE_VERSION = 1


class BundleSource(PersistedModel):
    """Where a bundle came from — shown before import so the user can sanity-check it."""

    machine: str = ""
    platform: str = ""
    profile: str = ""
    anki_version: str = ""
    exported_at: str = ""


class NoteTypeBundle(PersistedModel):
    """One note type's complete, portable Smart Notes setup.

    Attributes:
        bundle_version: :data:`BUNDLE_VERSION` at export time.
        source: Provenance, for the import preview.
        note_type_name: The note type's name on the source machine.
        anki_note_type: Anki's own note type dict (fields, templates, css). ``None`` when the
            bundle carries configuration only — for refreshing a note type that already exists
            on the target and must not have its templates overwritten.
        smart_notes: The Smart Notes configuration for that note type.
        deck_names: ``decks`` resolved to names, because the ids are per-collection.
        user_tools: ``{tool name: python source}`` for every ``user:`` tool the chains use.
    """

    bundle_version: int = BUNDLE_VERSION
    source: BundleSource = Field(default_factory=BundleSource)
    note_type_name: str
    anki_note_type: Optional[dict[str, Any]] = None
    smart_notes: SmartNotesNoteTypeConfig
    deck_names: list[str] = Field(default_factory=list)
    user_tools: dict[str, str] = Field(default_factory=dict)

    def field_names(self) -> list[str]:
        """The note type's field names, preferring the Anki schema's ORDER when present.

        The config only knows the fields it has rules for; the schema knows all of them, in the
        order the editor shows. An import mapping UI wants that order.
        """
        schema = self.anki_note_type or {}
        ordered = [
            str(f.get("name", "")) for f in schema.get("flds", []) if f.get("name")
        ]
        if ordered:
            return ordered
        names = [rule.field for rule in self.smart_notes.fields]
        if self.smart_notes.base_field:
            names.insert(0, self.smart_notes.base_field)
        return list(dict.fromkeys(names))

    def required_user_tools(self) -> list[str]:
        """The ``user:`` tool names the chains reference, in first-seen order."""
        seen: list[str] = []
        for rule in self.smart_notes.fields:
            for spec in rule.tools:
                if spec.tool.startswith(USER_TOOL_PREFIX) and spec.tool not in seen:
                    seen.append(spec.tool)
        return seen

    def missing_user_tools(self) -> list[str]:
        """Referenced ``user:`` tools whose source the bundle does NOT carry.

        Non-empty means the export could not read a tool file. Importing anyway leaves a chain
        that resolves to nothing on this machine, so the UI has to say so up front.
        """
        return [
            name for name in self.required_user_tools() if name not in self.user_tools
        ]

    def to_json(self) -> str:
        """Serialise to indented JSON (stable key order, so two exports diff cleanly)."""
        return json.dumps(self.dict(), ensure_ascii=False, indent=2, sort_keys=True)


class BundleError(ValueError):
    """A bundle could not be read (message is safe to show the user)."""


def parse_bundle(text: str) -> NoteTypeBundle:
    """Parse and validate bundle JSON.

    Args:
        text: The file's contents.

    Returns:
        The parsed :class:`NoteTypeBundle`.

    Raises:
        BundleError: The text is not JSON, is not a bundle, or was written by a NEWER Omnia.
    """
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise BundleError(f"This file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BundleError("This file does not contain an Omnia bundle.")

    version = raw.get("bundle_version")
    if not isinstance(version, int):
        raise BundleError(
            "This file is not an Omnia note-type bundle (no bundle_version)."
        )
    if version > BUNDLE_VERSION:
        raise BundleError(
            f"This bundle was written by a newer Omnia (format {version}; this one reads "
            f"{BUNDLE_VERSION}). Update Omnia on this machine, then import it again."
        )
    try:
        return NoteTypeBundle(**raw)
    except Exception as exc:  # pydantic ValidationError, kept generic for the message
        raise BundleError(f"This bundle is missing something it needs: {exc}") from exc
