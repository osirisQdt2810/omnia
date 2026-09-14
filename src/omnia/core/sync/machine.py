"""This machine's sync identity, and the switch that decides whether it answers at all.

Three things live here and nowhere else:

* the **key** that a machine ID carries, minted once per profile and regenerated on demand —
  regenerating is the revoke, and it is the only one there is;
* whether **sharing is on**, which is off in a fresh profile and stays off until somebody turns
  it on (ADR-020, condition 1);
* the **port** to bind, because a machine whose 8767 is taken by something else needs a way out
  that is not "reinstall".

All three are stored in ``machine.toml``, which is on disk beside the credentials rather than in
the collection. That is the whole point: ``features.toml`` rides the collection and therefore
AnkiWeb, so a switch stored there would turn sharing on over on the other machine too, and a key
stored there would hand two machines a single identity. Per-machine settings have to be per
machine, and the config layer routes the ``sync`` section accordingly.

Pure, apart from the repository it is handed — no ``aqt``, no sockets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.sync.pairing import (
    DEFAULT_PORT,
    PairingAddress,
    format_machine_id,
    format_passcode,
    new_token,
)
from omnia.core.sync.reachability import local_addresses, rank_addresses

if TYPE_CHECKING:  # typing only — nothing here needs the repository at import time
    from omnia.core.config.repository import ConfigRepository

SECTION = "sync"

__all__ = [
    "DEFAULT_PORT",
    "MachineIdentity",
    "MachineSettings",
    "access_code",
    "machine_id",
]


@dataclass(frozen=True)
class MachineIdentity:
    """What this machine is, for the purpose of being pulled from."""

    key: str
    port: int
    sharing: bool

    def address(self, host: str) -> PairingAddress:
        """The address another machine would dial, given the host to publish."""
        return PairingAddress(host=host, port=self.port, token=self.key)


class MachineSettings:
    """Reads and writes this machine's sync settings through the config repository.

    Args:
        repo: The config repository. Only the ``sync`` section is touched, and that section is
            routed to ``machine.toml`` — see the module docstring for why that matters. Typed
            rather than ``Any`` so a reader can follow where these values actually land: the
            routing is the whole of this module's correctness, and a hidden type is what let a
            backend that never read the section back ship green.
    """

    def __init__(self, repo: ConfigRepository) -> None:
        self._repo = repo

    def identity(self) -> MachineIdentity:
        """This machine's identity, minting a key the first time it is asked for.

        Minting on READ rather than at install: a profile that never opens the sync panel has no
        business holding a credential, and generating one lazily means the key's existence and
        the feature's use start at the same moment.
        """
        raw = self._section()
        key = str(raw.get("key", "") or "")
        if not key:
            key = new_token()
            self._write({"key": key})
        return MachineIdentity(
            key=key,
            port=_port(raw.get("port")),
            sharing=bool(raw.get("sharing", False)),
        )

    def regenerate(self) -> MachineIdentity:
        """Mint a new key, which stops every ID handed out before from opening anything."""
        self._write({"key": new_token()})
        return self.identity()

    def set_sharing(self, on: bool) -> None:
        """Remember whether this machine offers to be pulled from."""
        self._write({"sharing": bool(on)})

    def set_port(self, port: int) -> None:
        """Remember which port to offer on."""
        self._write({"port": _port(port)})

    def _section(self) -> dict[str, Any]:
        try:
            section = self._repo.raw_section(SECTION)
            return section if isinstance(section, dict) else {}
        except Exception:
            # A config that cannot be read must not stop the panel opening — it renders "not
            # ready" instead, which is a state it has to handle anyway.
            return {}

    def _write(self, values: dict[str, Any]) -> None:
        self._repo.update_section(SECTION, values)


def _port(value: Any) -> int:
    """A usable port, or the default — a hand-edited ``port = "nope"`` must not break sharing."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return number if 1 <= number <= 65535 else DEFAULT_PORT


def machine_id(
    identity: MachineIdentity, *, addresses: Optional[list[str]] = None
) -> str:
    """The number this machine shows, or "" when no other machine could dial it.

    The empty string is a real answer, not a failure: a machine with only loopback — no network,
    or a VPN that is not up — cannot be pulled from, and the panel says so instead of showing an
    ID that would never connect.

    Args:
        identity: This machine's port (the ID carries no secret — the access code is separate).
        addresses: Candidate addresses; discovered from the routing table when omitted.

    Returns:
        The ID, in groups of digits, or "".
    """
    ranked = rank_addresses(addresses if addresses is not None else local_addresses())
    if not ranked:
        return ""
    return format_machine_id(ranked[0].ip, identity.port)


def access_code(identity: MachineIdentity) -> str:
    """The code this machine shows beneath its ID, grouped for reading aloud."""
    return format_passcode(identity.key)
