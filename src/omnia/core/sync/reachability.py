"""Which of this machine's addresses another machine could actually dial.

A machine does not know its own reachable address. It knows the addresses of its interfaces,
and some of those are reachable from the other end and some are not — a loopback address never
is, a link-local one rarely is, and a private LAN address only is when both machines are on that
LAN. Guessing wrong produces a pairing code that looks perfectly fine and simply never connects,
which is the worst failure shape available: nothing to read, nothing to fix.

So this module ranks what it finds and says WHY each address is ranked where it is, and the
session shows the one it chose while letting the user pick another. The ranking prefers, in
order:

1. a **mesh address** — the 100.64.0.0/10 range a mesh VPN hands out. It is the only kind here
   that works when the two machines are not on the same network, which is the case this feature
   exists for;
2. a **private LAN address** (10/8, 172.16/12, 192.168/16), which works when they are;
3. anything else routable.

Loopback and link-local are excluded outright rather than ranked last: offering an address that
provably cannot work is offering a trap.

**None of this is shown to anyone.** The user handles one thing, their machine's ID, and the
best address is folded into it without being mentioned; these labels exist for the log and for
the person reading this code. That is also why the choice is made here rather than offered: a
picker of addresses is a question about networking, and the whole interface is built to never
ask one.

Pure stdlib, no ``aqt`` — so the ranking unit-tests headless against a fixed interface list.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

#: The shared-address range a mesh VPN allocates from (RFC 6598 carrier-grade NAT space). An
#: address in here is the strongest signal available that the machine is reachable from outside
#: its own network without anyone forwarding a port.
_MESH_RANGE = ipaddress.ip_network("100.64.0.0/10")

#: What each kind means, for the log and for whoever reads this next. Not rendered anywhere: see
#: the module docstring. Phrased about REACH rather than about which software provides it, so
#: that a line copied out of a log into a dialog by some future change still says nothing about
#: the underlying tool.
KIND_MESH = "reachable from anywhere both machines are signed in"
KIND_LAN = "reachable on this local network"
KIND_OTHER = "reachable if the other machine can route to it"


@dataclass(frozen=True)
class Address:
    """One address this machine might be reached at."""

    ip: str
    kind: str
    rank: int


def _classify(ip: str) -> Optional[Address]:
    """Rank one address, or return None when it provably cannot serve."""
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
        return None  # offering one of these is offering a trap
    if parsed.version == 4 and parsed in _MESH_RANGE:
        return Address(ip, KIND_MESH, 0)
    if parsed.is_private:
        return Address(ip, KIND_LAN, 1)
    return Address(ip, KIND_OTHER, 2)


def rank_addresses(candidates: Iterable[str]) -> list[Address]:
    """Return the dialable addresses among ``candidates``, best first.

    The caller takes the FIRST one and folds it into the machine's ID. The rest of the list is
    for the log, and for a future "this machine has no usable address" explanation.

    Args:
        candidates: Raw address strings, in any order, from any source.

    Returns:
        One :class:`Address` per usable candidate, sorted by how likely it is to work, with
        duplicates removed. Empty when nothing usable was offered — which is a real answer the
        caller must handle, not an error: a machine with only loopback has nothing to publish.
    """
    seen: set[str] = set()
    found: list[Address] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        address = _classify(text)
        if address is not None:
            found.append(address)
    return sorted(found, key=lambda address: (address.rank, address.ip))


def local_addresses() -> list[str]:
    """Every address this machine's own resolver reports for it.

    Best-effort and deliberately dumb: it asks the OS and hands the answers to
    :func:`rank_addresses`. Anything that raises is an empty list rather than an exception,
    because "we could not work out an address" is a state the session has to render anyway.
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.append(info[4][0])
    except Exception:
        return found
    return found
