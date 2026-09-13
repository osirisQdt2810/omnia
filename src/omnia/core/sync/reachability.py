"""Which of this machine's addresses another machine could actually dial.

A machine does not know its own reachable address. Guessing wrong produces an ID that looks
perfectly fine and simply never connects, which is the worst failure shape available: nothing to
read, nothing to fix. So this module does two things carefully — it FINDS candidates by asking
the routing table rather than the name resolver, and it RANKS them by how likely each is to be
reachable from the other end.

**Finding them.** Resolving the host name (``getaddrinfo(gethostname())``) is the obvious call
and the wrong one: it answers "what does this machine's NAME point at", which on macOS is
whatever mDNS advertises — typically the Wi-Fi address and not the VPN one, i.e. precisely the
address class this feature exists to use. On a box where the hostname maps to ``127.0.1.1`` it
answers loopback and nothing else. So :func:`local_addresses` asks the kernel instead: connecting
a UDP socket sends no packet, it only makes the kernel pick a route, and ``getsockname`` then
reports the source address that route would use. One probe per network worth reaching gives
exactly the candidates that matter.

**Ranking them.** In order: a **mesh** address (the shared-address range a mesh VPN allocates
from, and its IPv6 equivalent) — the only kind here that works when the two machines are not on
the same network; then a **unique-local** IPv6; then a **private LAN** address, which works when
they are; then anything else routable; and last, an address on a range a virtual-machine or
container bridge hands out, which is almost never reachable from another machine.

Loopback and link-local are excluded outright rather than ranked last: offering an address that
provably cannot work is offering a trap.

Ties keep DISCOVERY order rather than sorting by text. Sorting by the address string reads as
tidy and decides real cases by alphabet — ``172.17.0.1`` (a container bridge) sorts before
``192.168.1.40`` (the actual network), so the tidier code mints an ID nothing can dial.

**None of this is shown to anyone.** The user handles one thing, their machine's ID, and the best
address is folded into it without being mentioned; these labels exist for the log and for the
person reading this code. That is also why the choice is made here rather than offered: a picker
of addresses is a question about networking, and the whole interface is built to never ask one.

Pure stdlib, no ``aqt`` — so the ranking unit-tests headless against a fixed candidate list, and
the probing does against a stubbed socket.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

#: The shared-address range a mesh VPN allocates from (RFC 6598 carrier-grade NAT space), and the
#: unique-local prefix its IPv6 side uses. An address in either is the strongest signal available
#: that the machine is reachable from outside its own network with nothing forwarded.
_MESH_V4 = ipaddress.ip_network("100.64.0.0/10")
_MESH_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

#: Ranges a virtual-machine or container bridge hands out. An address here belongs to a network
#: that exists INSIDE this machine, so another machine cannot reach it. Ranked last rather than
#: dropped, unlike loopback: it is not impossible for one of these to be somebody's real LAN, and
#: ranked last is enough when the caller takes the first.
_VIRTUAL = (
    ipaddress.ip_network("172.17.0.0/16"),  # docker0, the default bridge
    ipaddress.ip_network("172.18.0.0/15"),  # docker's next allocations
    ipaddress.ip_network("192.168.56.0/24"),  # VirtualBox host-only
    ipaddress.ip_network("10.211.55.0/24"),  # Parallels host-only
    ipaddress.ip_network("10.37.129.0/24"),  # Parallels shared
    ipaddress.ip_network("192.168.64.0/24"),  # the macOS virtualisation framework
)

#: What each kind means, for the log and for whoever reads this next. Not rendered anywhere: see
#: the module docstring. Phrased about REACH rather than about which software provides it, so that
#: a line copied out of a log into a dialog by some future change still says nothing about the
#: underlying tool.
KIND_MESH = "reachable from anywhere both machines are signed in"
KIND_ULA = "reachable on a private network shared by both machines"
KIND_LAN = "reachable on this local network"
KIND_OTHER = "reachable if the other machine can route to it"
KIND_VIRTUAL = "a network that exists only inside this machine"

RANK_MESH = 0
RANK_ULA = 1
RANK_LAN = 2
RANK_OTHER = 3
RANK_VIRTUAL = 4

#: One probe per network worth reaching, in the order the answers should be preferred. The first
#: pair targets the service address that lives INSIDE the mesh range, which is what makes the mesh
#: route light up when there is one; the second pair is a public address, which produces the
#: default route. Connecting a UDP socket sends nothing and resolves nothing, so this costs
#: microseconds.
_PROBES = (
    (socket.AF_INET, "100.100.100.100"),
    (socket.AF_INET6, "fd7a:115c:a1e0::53"),
    (socket.AF_INET, "8.8.8.8"),
    (socket.AF_INET6, "2001:4860:4860::8888"),
)


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
    if parsed.version == 4 and parsed in _MESH_V4:
        return Address(ip, KIND_MESH, RANK_MESH)
    if parsed.version == 6 and parsed in _MESH_V6:
        return Address(ip, KIND_MESH, RANK_MESH)
    if any(
        parsed.version == network.version and parsed in network for network in _VIRTUAL
    ):
        return Address(ip, KIND_VIRTUAL, RANK_VIRTUAL)
    if parsed.version == 6 and parsed.is_private:
        return Address(ip, KIND_ULA, RANK_ULA)
    if parsed.is_private:
        return Address(ip, KIND_LAN, RANK_LAN)
    return Address(ip, KIND_OTHER, RANK_OTHER)


def rank_addresses(candidates: Iterable[str]) -> list[Address]:
    """Return the dialable addresses among ``candidates``, best first.

    The caller takes the FIRST one and folds it into the machine's ID. The rest of the list is
    for the log, and for a future "this machine has no usable address" explanation.

    Args:
        candidates: Raw address strings, in any order, from any source.

    Returns:
        One :class:`Address` per usable candidate, best first, with duplicates removed and
        discovery order preserved within a rank. Empty when nothing usable was offered — a real
        answer the caller must handle, not an error: a machine with only loopback has nothing to
        publish.
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
    # `sorted` is stable, so equal ranks keep the order they were discovered in — see the module
    # docstring for why sorting by the address text is worse than not sorting at all.
    return sorted(found, key=lambda address: address.rank)


def _probe(family: int, target: str) -> Optional[str]:
    """The source address this machine would use to reach ``target``, or None when no route."""
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))  # the discard port; a UDP connect sends no packet
        return str(sock.getsockname()[0])
    except OSError:
        return None  # no route on that family or network, which is an ordinary state
    finally:
        sock.close()


def local_addresses() -> list[str]:
    """Every address this machine would use as a source, one per network worth reaching.

    Asks the ROUTING TABLE, not the name resolver — see the module docstring for why the obvious
    call answers a different question. No name lookup happens and no packet is sent.

    It is still I/O against the kernel's networking stack, and the only caller that matters is a
    dialog rendering this machine's ID. If it ever stops being instant on some machine it belongs
    on ``QueryOp``/``mw.taskman``, like every other blocking call (CONVENTIONS.md, Threading),
    rather than on the Qt main thread.

    Returns:
        The source addresses in probe order — mesh first, then the default route — with
        duplicates removed. Empty when the machine has no route at all, which
        :func:`rank_addresses` turns into "nothing to publish" rather than an error.
    """
    found: list[str] = []
    for family, target in _PROBES:
        address = _probe(family, target)
        if address and address not in found:
            found.append(address)
    return found
