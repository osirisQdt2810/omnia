"""Moving one note type's Smart Notes setup between collections (export / import).

Smart Notes settings live in the collection config and therefore sync — but only within ONE
AnkiWeb account, and only as a whole. Carrying a single note type's setup to another machine,
another profile, or another person had no route at all: the note type, the per-field rules with
their prompts and tool chains, the dependency graph and the user-authored tools each live
somewhere different, and copying any one of them alone produces a configuration that loads and
does not work.

* :mod:`.bundle` — the portable JSON format (note type schema + config + user tools + deck
  names) and its version gate.
* :mod:`.remap` — rewriting a configuration onto different field names, consistently across
  every place a field name is written down.
* :mod:`.collection` — the only part that touches Anki: reading a bundle out of a collection
  and applying one into it.
"""

from omnia.plugins.smart_notes.transfer.bundle import (
    BUNDLE_VERSION,
    BundleError,
    BundleSource,
    NoteTypeBundle,
    parse_bundle,
)
from omnia.plugins.smart_notes.transfer.remap import (
    RemapReport,
    remap_note_type_config,
    suggest_renames,
)

__all__ = [
    "BUNDLE_VERSION",
    "BundleError",
    "BundleSource",
    "NoteTypeBundle",
    "RemapReport",
    "parse_bundle",
    "remap_note_type_config",
    "suggest_renames",
]
