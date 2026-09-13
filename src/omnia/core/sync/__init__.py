"""Pulling a chosen part of one machine's Omnia setup onto another (ADR-020).

The transport is deliberately dumb — HTTP and JSON, read-only, one session at a time — so that
everything interesting stays here as pure logic: what a source machine offers, what of it
applies to the target, and what has to be skipped and why.

This package holds the parts that need no Anki:

* :mod:`.pairing` — the code one machine shows and the other types in (address + session key);
* :mod:`.reachability` — which of this machine's addresses another one could actually dial.

Nothing here imports ``aqt`` or ``anki``.
"""

from __future__ import annotations

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

__all__ = [
    "GROUP",
    "KIND_LAN",
    "KIND_MESH",
    "KIND_OTHER",
    "KIND_ULA",
    "KIND_VIRTUAL",
    "TOKEN_CHARS",
    "Address",
    "PairingAddress",
    "PairingError",
    "format_pairing_code",
    "local_addresses",
    "new_token",
    "parse_pairing_code",
    "rank_addresses",
]
