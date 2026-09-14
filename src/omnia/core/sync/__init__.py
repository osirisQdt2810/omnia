"""Pulling a chosen part of one machine's Omnia setup onto another (ADR-020).

The transport is deliberately dumb — HTTP and JSON, read-only, one session at a time — so that
everything interesting stays here as pure logic: what a source machine offers, what of it
applies to the target, and what has to be skipped and why.

This package holds the parts that need no Anki:

* :mod:`.pairing` — the number one machine shows and the code that opens it;
* :mod:`.reachability` — which of this machine's addresses another one could actually dial;
* :mod:`.inventory` — what a source offers, as data;
* :mod:`.tree` — those decks as a tree, and what picking one of them means;
* :mod:`.selection` — the whole of what is being asked for: decks, the note types they drag along,
  and the settings;
* :mod:`.service` — the source's read-only session (ADR-020's five conditions, in code);
* :mod:`.clash` — what a pull would land on top of, and what happens to a note that is on both;
* :mod:`.package` — what one machine asks another to pack, and what that request may mean;
* :mod:`.progress` — how far a pull has got and how much longer it has, as a value;
* :mod:`.client` — the target's side, whose whole job is that every failure names its own fix;
* :mod:`.machine` — this machine's key, port and sharing switch, kept OFF the collection.

Nothing here imports ``aqt`` or ``anki``: the service takes a callable that reads the collection,
and marshalling that onto the Qt main thread is the caller's job.
"""

from __future__ import annotations

from omnia.core.sync.clash import (
    KEEP,
    OVERRIDE,
    Clashes,
    NoteTypeClash,
    find_clashes,
)
from omnia.core.sync.client import (
    PACK_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    SyncClient,
    SyncError,
)
from omnia.core.sync.inventory import (
    PROTOCOL,
    ConfigSummary,
    DeckEntry,
    Inventory,
    InventoryError,
    NoteTypeEntry,
)
from omnia.core.sync.machine import (
    MachineIdentity,
    MachineSettings,
    access_code,
    machine_id,
)
from omnia.core.sync.package import (
    PackageError,
    PackageOffer,
    PackageRequest,
)
from omnia.core.sync.pairing import (
    DEFAULT_PORT,
    GROUP,
    ID_DIGITS,
    PASSCODE_DIGITS,
    PairingAddress,
    PairingError,
    format_machine_id,
    format_passcode,
    new_token,
    parse_machine_id,
    parse_passcode,
)
from omnia.core.sync.progress import (
    DONE,
    DOWNLOADING,
    FAILED,
    IMPORTING,
    PREPARING,
    Progress,
    ProgressTracker,
    format_bytes,
    format_duration,
)
from omnia.core.sync.reachability import (
    KIND_LAN,
    KIND_MESH,
    KIND_OTHER,
    KIND_ULA,
    KIND_VIRTUAL,
    Address,
    local_addresses,
    rank_addresses,
)
from omnia.core.sync.selection import (
    DROPPED,
    IDLE,
    NEEDED,
    OfferSelection,
)
from omnia.core.sync.service import (
    HELLO_PATH,
    INVENTORY_PATH,
    LOCKOUT_SECONDS,
    MAX_ATTEMPTS,
    PACKAGE_PATH,
    TOKEN_HEADER,
    Session,
)
from omnia.core.sync.tree import (
    PARTIAL,
    PICKED,
    UNPICKED,
    DeckNode,
    DeckRow,
    DeckSelection,
    DeckTree,
)

__all__ = [
    "DEFAULT_PORT",
    "DONE",
    "DOWNLOADING",
    "DROPPED",
    "FAILED",
    "GROUP",
    "HELLO_PATH",
    "IDLE",
    "ID_DIGITS",
    "IMPORTING",
    "INVENTORY_PATH",
    "KEEP",
    "KIND_LAN",
    "KIND_MESH",
    "KIND_OTHER",
    "KIND_ULA",
    "KIND_VIRTUAL",
    "LOCKOUT_SECONDS",
    "MAX_ATTEMPTS",
    "NEEDED",
    "OVERRIDE",
    "PACKAGE_PATH",
    "PACK_TIMEOUT_SECONDS",
    "PARTIAL",
    "PASSCODE_DIGITS",
    "PICKED",
    "PREPARING",
    "PROTOCOL",
    "TIMEOUT_SECONDS",
    "TOKEN_HEADER",
    "UNPICKED",
    "Address",
    "Clashes",
    "ConfigSummary",
    "DeckEntry",
    "DeckNode",
    "DeckRow",
    "DeckSelection",
    "DeckTree",
    "Inventory",
    "InventoryError",
    "MachineIdentity",
    "MachineSettings",
    "NoteTypeClash",
    "NoteTypeEntry",
    "OfferSelection",
    "PackageError",
    "PackageOffer",
    "PackageRequest",
    "PairingAddress",
    "PairingError",
    "Progress",
    "ProgressTracker",
    "Session",
    "SyncClient",
    "SyncError",
    "access_code",
    "find_clashes",
    "format_bytes",
    "format_duration",
    "format_machine_id",
    "format_passcode",
    "local_addresses",
    "machine_id",
    "new_token",
    "parse_machine_id",
    "parse_passcode",
    "rank_addresses",
]
