"""A machine's ID: the one string a user ever handles, and everything it has to carry.

The user's model is exactly two steps — each machine shows an ID, and you type the other
machine's ID into yours. Nothing about addresses, ports, keys, or what makes the two machines
able to reach each other. That is the requirement, and this module is where it meets the fact
that there is no rendezvous server (ADR-020): nothing exists that could turn a short numeric
code into an address, so the ID has to CARRY the address itself.

Everything about its shape follows from that:

* it holds ``host``, ``port`` and a ``token``, and nothing else, packed as BYTES rather than as
  text — an IPv4 address is four bytes, not fourteen characters, and a 128-bit key is sixteen
  bytes, not thirty-two hex characters. Spelling them out cost fifty characters of an ID that a
  person has to read off one screen and type into another;
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
import socket
from dataclasses import dataclass

#: How many characters of randomness the machine key carries. 32 hex characters is 128 bits:
#: far past guessing, and — packed as the sixteen bytes it actually is — short enough that the
#: whole ID stays under forty characters.
TOKEN_CHARS = 32

_TOKEN_BYTES = TOKEN_CHARS // 2

#: How the host is packed, so an address that is four bytes takes four bytes. The tag is the
#: first byte of every code, which is also what makes an ID from a future Omnia recognisable as
#: one rather than as a mistyped string.
_KIND_IPV4 = 4
_KIND_IPV6 = 6
_KIND_NAME = (
    0  # a DNS name, length-prefixed — nothing produces one today, but an ID is a
)
#: format, and refusing to carry a name would make one impossible to add later.

#: The code is printed in groups of this many characters, separated by dashes. Purely cosmetic —
#: :func:`parse_pairing_code` strips them — but it is what makes a forty-character string
#: something a person can read back without losing their place.
GROUP = 5

_TOKEN_RE = re.compile(rf"^[0-9a-f]{{{TOKEN_CHARS}}}$")
# 253 is the maximum length of a DNS name; a shorter cap refuses a legitimate long hostname
# with "not a usable address" and nothing the user could do about it.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")


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
    encoded = base64.b32encode(_pack(address)).decode("ascii").rstrip("=")
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
        raw = base64.b32decode(cleaned + padding)
    except Exception as exc:  # a truncated or mistyped code lands here
        raise PairingError("that code is incomplete or was mistyped") from exc
    return _unpack(raw)


def _pack(address: PairingAddress) -> bytes:
    """``host``/``port``/``token`` as bytes: a tag, the host, two bytes of port, the key."""
    try:
        host = socket.inet_pton(socket.AF_INET, address.host)
        kind = _KIND_IPV4
    except OSError:
        try:
            host = socket.inet_pton(socket.AF_INET6, address.host)
            kind = _KIND_IPV6
        except OSError:
            name = address.host.encode("utf-8")[:253]
            host = bytes([len(name)]) + name
            kind = _KIND_NAME
    return (
        bytes([kind])
        + host
        + int(address.port).to_bytes(2, "big")
        + bytes.fromhex(address.token)
    )


def _unpack(raw: bytes) -> PairingAddress:
    """The inverse of :func:`_pack`, with every way it can be wrong given its own sentence."""
    if not raw:
        raise PairingError("that code is incomplete or was mistyped")
    kind, rest = raw[0], raw[1:]
    try:
        if kind == _KIND_IPV4:
            host, rest = socket.inet_ntop(socket.AF_INET, rest[:4]), rest[4:]
        elif kind == _KIND_IPV6:
            host, rest = socket.inet_ntop(socket.AF_INET6, rest[:16]), rest[16:]
        elif kind == _KIND_NAME:
            length = rest[0]
            host, rest = rest[1 : 1 + length].decode("utf-8"), rest[1 + length :]
        else:
            # A tag this build has no meaning for is a code from a DIFFERENT Omnia, not a typo.
            # Not "newer": an unknown tag cannot tell the two directions apart, and guessing
            # would send half the people who see it to update the wrong machine. What it can
            # say is that re-reading the ID will not help, which is the useful half.
            raise PairingError("that code was made by a different version of Omnia")
    except PairingError:
        raise
    except (IndexError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise PairingError("that code is incomplete or was mistyped") from exc
    if len(rest) != 2 + _TOKEN_BYTES:
        raise PairingError("that code is incomplete or was mistyped")
    return PairingAddress(
        host=host,
        port=int.from_bytes(rest[:2], "big"),
        token=rest[2:].hex(),
    )
