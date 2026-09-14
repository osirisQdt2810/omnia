"""A machine's ID and its access code: the two things a user ever handles.

The model is the one people already know from remote-desktop tools: each machine shows a
**number**, you type the other machine's number into yours, and a short **code** proves you are
allowed in. Nothing about addresses, ports or what makes the two machines able to reach each
other.

Why two fields rather than one string is the whole design, and it comes from ADR-020: there is
no rendezvous server. AnyDesk's nine digits are short because AnyDesk runs infrastructure that
turns them into an address; nothing here does, so **the ID has to BE the address**. An IPv4
address is a 32-bit number — ten digits, plus one check digit — and that is the entire budget.
A key cannot also fit, so it is the second field, exactly as an unattended-access password is in
those tools.

What follows from that:

* **The ID is eleven digits** for the ordinary case (a v4 address on the default port), shown in
  groups of three. It is a number, not a string: no case to get wrong, no letters that look like
  digits, and a phone keypad can type it.
* **The check digit is load-bearing.** Without it a single mistyped digit is a different, equally
  valid address, and the user gets "could not connect" for a machine that is fine. With it they
  get "that ID was mistyped", which is the answer they can act on.
* **The access code is nine digits**, generated, and safe only because the service refuses to be
  guessed at — :mod:`omnia.core.sync.service` locks out after a few wrong ones. A billion
  combinations against five attempts a minute is centuries; without the lockout it would be an
  afternoon. The two halves are one design and neither works alone.
* **Regenerating the code is the revoke**, and the only one there is.

Everything here is pure — no sockets, no ``aqt`` — so the format unit-tests headless and both
ends share one implementation of it.
"""

from __future__ import annotations

import re
import secrets
import socket
from dataclasses import dataclass

#: The port a machine offers on unless its owner changed it. An ID for this port says nothing
#: about it, which is what keeps the common case eleven digits; any other port is spelled out
#: and the ID is longer. Defined HERE rather than in :mod:`omnia.core.sync.machine` because the
#: ID's length depends on it and this module may not import that one.
DEFAULT_PORT = 8767

#: Digits in the ordinary ID: ten for the address, one to catch a typo.
ID_DIGITS = 11

#: Digits in the access code.
PASSCODE_DIGITS = 9

#: Digits per group when either is displayed. Cosmetic — both parsers strip the separators —
#: but it is what makes a number readable back over a phone call without losing your place.
GROUP = 3

#: How a non-default port is carried: the address, then the port, then the check digit. Longer
#: than eleven digits on purpose — a machine whose owner moved the port off the default has
#: already left the ordinary case, and a silent truncation would be worse than four more digits.
_PORT_DIGITS = 5

_DIGITS_RE = re.compile(r"^[0-9]+$")
_PASSCODE_RE = re.compile(rf"^[0-9]{{{PASSCODE_DIGITS}}}$")


class PairingError(ValueError):
    """An ID or access code that cannot be used, with a reason a person can act on."""


@dataclass(frozen=True)
class PairingAddress:
    """What an ID and an access code together name: where to reach a machine, and its code."""

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
        if not _is_ip(self.host):
            raise PairingError(f"{self.host!r} is not a usable address")
        if not 1 <= int(self.port) <= 65535:
            raise PairingError(f"{self.port} is not a usable port")
        if not _PASSCODE_RE.match(self.token or ""):
            raise PairingError("that is not a usable access code")

    @property
    def base_url(self) -> str:
        """The root URL of the source machine's session service."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


def new_token() -> str:
    """Return a fresh access code: nine digits, zero-padded.

    Minted when a profile first offers to share, and again on every regenerate — which is the
    revoke, and the only one there is. Nine digits is safe only because the service refuses to
    be guessed at; see the module docstring, and :mod:`omnia.core.sync.service`.
    """
    return f"{secrets.randbelow(10**PASSCODE_DIGITS):0{PASSCODE_DIGITS}d}"


def format_passcode(token: str) -> str:
    """Render an access code for reading aloud: ``123 456 789``."""
    return " ".join(
        token[index : index + GROUP] for index in range(0, len(token), GROUP)
    )


def parse_passcode(text: str) -> str:
    """Read an access code back, forgiving spaces, dashes and line breaks.

    Raises:
        PairingError: When it is not nine digits.
    """
    cleaned = re.sub(r"[\s\-]+", "", str(text or ""))
    if not cleaned:
        raise PairingError("no access code was entered")
    if not _PASSCODE_RE.match(cleaned):
        raise PairingError(f"an access code is {PASSCODE_DIGITS} digits")
    return cleaned


def format_machine_id(host: str, port: int = DEFAULT_PORT) -> str:
    """Render the number this machine shows.

    Args:
        host: The address another machine would dial. Must be an IP — a name cannot be a number,
            and nothing produces one (addresses come from the routing table).
        port: The port it offers on.

    Returns:
        The ID in groups of three digits.

    Raises:
        PairingError: When ``host`` is not an address that fits in an ID.
    """
    digits = _address_digits(host)
    if int(port) != DEFAULT_PORT:
        digits += f"{int(port):0{_PORT_DIGITS}d}"
    return _group(digits + str(_check_digit(digits)))


def parse_machine_id(text: str) -> tuple[str, int]:
    """Read a machine ID back into ``(host, port)``.

    Tolerant of how an ID arrives — spaces, dashes and line breaks are stripped — because it
    travels through chat windows and handwriting, and refusing one over the user's formatting
    rather than its content is refusing it for the wrong reason.

    Args:
        text: What the user typed.

    Returns:
        The address it names.

    Raises:
        PairingError: When it is not an ID, or is one that was mistyped.
    """
    cleaned = re.sub(r"[\s\-.]+", "", str(text or ""))
    if not cleaned:
        raise PairingError("no ID was entered")
    if not _DIGITS_RE.match(cleaned):
        raise PairingError("an ID is only digits")
    body = cleaned[:-1]
    if not body or _fold(cleaned) != 0:
        # The whole reason for the check digit: without it one wrong digit is a DIFFERENT valid
        # address, and the user spends an evening on "could not connect" for a machine that was
        # never asked.
        raise PairingError("that ID was mistyped — check it against the other computer")
    if len(body) == 10:
        return _address_from_digits(body), DEFAULT_PORT
    if len(body) == 10 + _PORT_DIGITS:
        port = int(body[10:])
        if not 1 <= port <= 65535:
            raise PairingError("that ID carries no usable port")
        return _address_from_digits(body[:10]), port
    if len(body) == 39 or len(body) == 39 + _PORT_DIGITS:
        port = int(body[39:]) if len(body) > 39 else DEFAULT_PORT
        return _address_from_digits(body[:39], version=6), port
    raise PairingError(
        "that ID is incomplete or was made by a different version of Omnia"
    )


def _group(digits: str) -> str:
    """``"16860441953"`` → ``"168 604 419 53"``."""
    return " ".join(
        digits[index : index + GROUP] for index in range(0, len(digits), GROUP)
    )


#: Verhoeff's check digit, built on the dihedral group D5 (1969). Chosen over the obvious
#: weighted sum and over Luhn — the credit-card one — because it catches EVERY single-digit slip
#: and EVERY adjacent transposition, which are the two ways a person mistypes a number they are
#: reading off another screen. Luhn misses a transposed 0 and 9; a weighted sum mod 10 misses
#: whole classes, because even weights are not invertible there.
#:
#: The group table is CONSTRUCTED rather than pasted: ``d(a, b)`` composes two of the ten
#: symmetries of a pentagon (0-4 rotations, 5-9 reflections), and getting a hundred hand-typed
#: numbers right is not something to trust. Only the permutation below is data, and the tests
#: verify the whole thing exhaustively rather than taking it on faith.
_P1 = (1, 5, 7, 6, 2, 8, 3, 0, 9, 4)


def _d5(a: int, b: int) -> int:
    """Compose two symmetries of a pentagon."""
    if a < 5:
        return (a + b) % 5 if b < 5 else 5 + (a + b) % 5
    return 5 + (a - b) % 5 if b < 5 else (a - b) % 5


def _shuffle(position: int, digit: int) -> int:
    """Apply the permutation ``position`` times — this is what makes ORDER matter."""
    for _ in range(position % 8):
        digit = _P1[digit]
    return digit


def _fold(digits: str) -> int:
    """Compose every digit into one symmetry. Zero exactly when a complete ID is intact.

    POSITION is what carries the transposition property — the permutation is applied once per
    place counting from the RIGHT — so the check digit has to be folded in at place zero, the
    same place it was generated for. Off by one and single-digit slips are still caught while
    two swapped neighbours sail through, which is the harder mistake to spot by eye.
    """
    interim = 0
    for position, digit in enumerate(reversed(digits)):
        interim = _d5(interim, _shuffle(position, int(digit)))
    return interim


def _check_digit(body: str) -> int:
    """The digit that makes ``body + digit`` fold to zero."""
    return _INVERSE[_fold(body + "0")]


#: The inverse of each symmetry: a rotation undone by the opposite rotation, a reflection by
#: itself. Written out because it is five lines of code for ten obvious numbers.
_INVERSE = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _is_ip(host: str) -> bool:
    """Whether ``host`` is an IP literal this module can turn into digits."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host or "")
            return True
        except OSError:
            continue
    return False


def _address_digits(host: str) -> str:
    """An IP as decimal digits: ten for v4, thirty-nine for v6, zero-padded either way."""
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
        width = 10
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, host)
            width = 39
        except OSError as exc:
            raise PairingError(f"{host!r} is not an address an ID can carry") from exc
    return f"{int.from_bytes(packed, 'big'):0{width}d}"


def _address_from_digits(digits: str, *, version: int = 4) -> str:
    """The inverse of :func:`_address_digits`."""
    size = 4 if version == 4 else 16
    try:
        packed = int(digits).to_bytes(size, "big")
    except (OverflowError, ValueError) as exc:
        raise PairingError("that ID does not name a usable address") from exc
    family = socket.AF_INET if version == 4 else socket.AF_INET6
    return str(socket.inet_ntop(family, packed))
