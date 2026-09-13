"""A machine's ID: the one string a user ever handles, and everything it has to carry.

The user's model is exactly two steps — each machine shows an ID, and you type the other
machine's ID into yours. Nothing about addresses, ports, keys, or what makes the two machines
able to reach each other. That is the requirement, and this module is where it meets the fact
that there is no rendezvous server (ADR-020): nothing exists that could turn a short numeric
code into an address, so the ID has to CARRY the address itself.

Everything about its shape follows from that:

* it holds ``host``, ``port`` and a ``token``, and nothing else;
* it is base32 without padding, upper-cased and grouped, so it reads over a phone call and
  survives a copy-paste that eats whitespace or changes case — unlike a raw ``100.x.y.z:8767``
  URL, which invites being typed into a browser and looks like something to keep;
* it is **opaque**. A user who can read an address out of their ID will start reasoning about
  addresses, and the next question is which tool provides them — which is precisely what the
  interface must never bring up.

So an ID is a credential wearing the clothes of an identifier. It is stable per machine, not
per session: the shape this feature serves is a second machine pulling from a main machine that
is simply switched on, and a per-session code would need someone sitting at the source to read
it out every time. :func:`new_token` mints the key; regenerating it is what revokes every ID
handed out before.

Everything here is pure — no sockets, no ``aqt`` — so the format unit-tests headless and both
ends share one implementation of it.
"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass

#: How many characters of randomness the machine key carries. 32 hex characters is 128 bits:
#: far past guessing, and short enough that the whole ID stays about forty characters.
TOKEN_CHARS = 32

#: The code is printed in groups of this many characters, separated by dashes. Purely cosmetic —
#: :func:`parse_pairing_code` strips them — but it is what makes a forty-character string
#: something a person can read back without losing their place.
GROUP = 5

_TOKEN_RE = re.compile(rf"^[0-9a-f]{{{TOKEN_CHARS}}}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,64}$")
_SEPARATOR = "|"


class PairingError(ValueError):
    """A pairing code that cannot be read, with a reason a person can act on."""


@dataclass(frozen=True)
class PairingAddress:
    """What a machine ID decodes to: where to reach it, and the key it will demand."""

    host: str
    port: int
    token: str

    def __post_init__(self) -> None:
        """Validate the three parts together.

        Raises:
            PairingError: When any part is missing or out of range. Constructed rather than
                validated later because an address that cannot be dialled is not an address:
                every consumer would otherwise have to re-check the same three things.
        """
        if not _HOST_RE.match(self.host or ""):
            raise PairingError(f"{self.host!r} is not a usable address")
        if not 1 <= int(self.port) <= 65535:
            raise PairingError(f"{self.port} is not a usable port")
        if not _TOKEN_RE.match(self.token or ""):
            raise PairingError("the code carries no usable key")

    @property
    def base_url(self) -> str:
        """The root URL of the source machine's session service."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


def new_token() -> str:
    """Return a fresh machine key (128 bits, hex).

    Called once when a profile first offers to share, and again on every regenerate — which is
    the revoke: every ID handed out before stops opening anything.
    """
    return secrets.token_hex(TOKEN_CHARS // 2)


def format_pairing_code(address: PairingAddress) -> str:
    """Render ``address`` as the ID this machine shows.

    Args:
        address: Where this machine is reachable, and its key.

    Returns:
        An upper-case base32 string in dash-separated groups.
    """
    raw = _SEPARATOR.join([address.host, str(address.port), address.token])
    encoded = base64.b32encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return "-".join(
        encoded[index : index + GROUP] for index in range(0, len(encoded), GROUP)
    )


def parse_pairing_code(code: str) -> PairingAddress:
    """Read a machine ID back into an address.

    Tolerant of how an ID arrives: dashes, spaces, line breaks and lower case are all stripped
    before decoding, because an ID travels through chat windows and handwriting, and refusing
    one over a stray space would be refusing it for the user's formatting rather than for its
    content.

    Args:
        code: What the user pasted.

    Returns:
        The address it names.

    Raises:
        PairingError: When it is not a code, or not one this build understands.
    """
    cleaned = re.sub(r"[\s\-]+", "", str(code or "")).upper()
    if not cleaned:
        raise PairingError("no code was entered")
    if not re.fullmatch(r"[A-Z2-7]+", cleaned):
        raise PairingError("that does not look like a pairing code")
    padding = "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned + padding).decode("utf-8")
    except Exception as exc:  # a truncated or mistyped code lands here
        raise PairingError("that code is incomplete or was mistyped") from exc
    parts = raw.split(_SEPARATOR)
    if len(parts) != 3:
        raise PairingError("that code was made by a different version of Omnia")
    host, port, token = parts
    try:
        number = int(port)
    except ValueError as exc:
        raise PairingError("that code carries no usable port") from exc
    return PairingAddress(host=host, port=number, token=token)
