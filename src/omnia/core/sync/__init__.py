"""Pulling a chosen part of one machine's Omnia setup onto another (ADR-020).

The transport is deliberately dumb — HTTP and JSON, read-only, one session at a time — so that
everything interesting stays here as pure logic: what a source machine offers, what of it
applies to the target, and what has to be skipped and why.

This package holds the parts that need no Anki:

* :mod:`.pairing` — the ID one machine shows and the other types in (address + machine key);
* :mod:`.reachability` — which of this machine's addresses another one could actually dial;
* :mod:`.inventory` — what a source offers, as data;
* :mod:`.service` — the source's read-only session (ADR-020's five conditions, in code);
* :mod:`.client` — the target's side, whose whole job is that every failure names its own fix;
* :mod:`.machine` — this machine's key, port and sharing switch, kept OFF the collection.

Nothing here imports ``aqt`` or ``anki``: the service takes a callable that reads the collection,
and marshalling that onto the Qt main thread is the caller's job.
"""

from __future__ import annotations

from omnia.core.sync.client import (
    TIMEOUT_SECONDS,
    SyncClient,
    SyncError,
    check,
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
    DEFAULT_PORT,
    MachineIdentity,
    MachineSettings,
    machine_id,
)
from omnia.core.sync.pairing import (
    GROUP,
    TOKEN_CHARS,
    PairingAddress,
    PairingError,
    format_pairing_code,
    new_token,
    parse_pairing_code,
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
from omnia.core.sync.service import (
    HELLO_PATH,
    INVENTORY_PATH,
    TOKEN_HEADER,
    Session,
)

__all__ = [
    "DEFAULT_PORT",
    "GROUP",
    "HELLO_PATH",
    "INVENTORY_PATH",
    "KIND_LAN",
    "KIND_MESH",
    "KIND_OTHER",
    "KIND_ULA",
    "KIND_VIRTUAL",
    "PROTOCOL",
    "TIMEOUT_SECONDS",
    "TOKEN_CHARS",
    "TOKEN_HEADER",
    "Address",
    "ConfigSummary",
    "DeckEntry",
    "Inventory",
    "InventoryError",
    "MachineIdentity",
    "MachineSettings",
    "NoteTypeEntry",
    "PairingAddress",
    "PairingError",
    "Session",
    "SyncClient",
    "SyncError",
    "check",
    "format_pairing_code",
    "local_addresses",
    "machine_id",
    "new_token",
    "parse_pairing_code",
    "rank_addresses",
]
