"""Tests for a machine's ID: the one string a user ever handles.

There is no rendezvous server (ADR-020), so the ID must carry the address — which makes it a
credential wearing the clothes of an identifier, and decides everything about its shape. It is
opaque so nobody starts reasoning about the address inside it; it forgives how an ID travels
(dashes, spaces, case) because it travels through chat windows and handwriting; and it refuses
outright anything it cannot turn back into an address, with a reason rather than a stack trace.

Which addresses are worth putting in one is :mod:`omnia.core.sync.reachability`'s problem, and
is tested next door.
"""

from __future__ import annotations

import pytest

from omnia.core.sync import (
    TOKEN_CHARS,
    PairingAddress,
    PairingError,
    format_pairing_code,
    new_token,
    parse_pairing_code,
)


def _address(host: str = "100.71.161.7", port: int = 8767) -> PairingAddress:
    return PairingAddress(host=host, port=port, token=new_token())


class TestTheCodeRoundTrips:
    def test_what_goes_in_comes_back(self):
        address = _address()

        assert parse_pairing_code(format_pairing_code(address)) == address

    @pytest.mark.parametrize(
        "host",
        ["100.71.161.7", "192.168.1.40", "omnia-desk.local", "fd7a:115c:a1e0::1"],
    )
    def test_every_shape_of_address_survives(self, host):
        address = PairingAddress(host=host, port=8767, token=new_token())

        assert parse_pairing_code(format_pairing_code(address)).host == host

    def test_the_code_carries_no_readable_address(self):
        # It is a credential, not a label. A raw "100.x.y.z:8767" invites being pasted into a
        # browser and kept; an opaque string reads as something that belongs to one session.
        code = format_pairing_code(_address())

        assert "100.71" not in code
        assert "8767" not in code
        assert ":" not in code and "/" not in code

    def test_it_is_grouped_for_reading_aloud(self):
        code = format_pairing_code(_address())

        assert "-" in code
        assert all(len(group) <= 5 for group in code.split("-"))


class TestHowACodeArrives:
    """A code travels through chat windows and handwriting; refusing one over a stray space
    would be refusing it for the user's formatting rather than its content."""

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda code: code.lower(),
            lambda code: code.replace("-", ""),
            lambda code: code.replace("-", " "),
            lambda code: f"  {code}\n",
            lambda code: code.replace("-", "\n"),
        ],
    )
    def test_formatting_damage_is_forgiven(self, mangle):
        address = _address()

        assert parse_pairing_code(mangle(format_pairing_code(address))) == address

    def test_an_empty_code_says_so(self):
        with pytest.raises(PairingError, match="no code"):
            parse_pairing_code("   ")

    def test_something_that_is_not_a_code_says_so(self):
        with pytest.raises(PairingError, match="does not look like"):
            parse_pairing_code("http://100.71.161.7:8767")

    def test_a_truncated_code_is_refused_rather_than_half_read(self):
        code = format_pairing_code(_address())

        with pytest.raises(PairingError):
            parse_pairing_code(code[: len(code) // 2])

    def test_a_code_from_another_shape_is_named_as_such(self):
        import base64

        # The text shape the first version of this used. Its leading byte is not one of the
        # host tags, so it reads as another Omnia's code rather than as a typo — which is the
        # difference between updating a machine and re-reading a correct ID for an hour.
        raw = base64.b32encode(b"100.71.161.7|8767").decode().rstrip("=")

        with pytest.raises(PairingError, match="different version"):
            parse_pairing_code(raw)


class TestAnAddressThatCannotWork:
    def test_a_port_out_of_range_is_refused(self):
        with pytest.raises(PairingError, match="port"):
            PairingAddress(host="100.71.161.7", port=70000, token=new_token())

    def test_a_missing_key_is_refused(self):
        # Without the token the code would open a session for anyone who guessed the address.
        with pytest.raises(PairingError, match="key"):
            PairingAddress(host="100.71.161.7", port=8767, token="")

    def test_a_short_key_is_refused(self):
        with pytest.raises(PairingError, match="key"):
            PairingAddress(host="100.71.161.7", port=8767, token="abc123")

    def test_the_base_url_brackets_an_ipv6_host(self):
        address = PairingAddress(host="fd7a:115c::1", port=8767, token=new_token())

        assert address.base_url == "http://[fd7a:115c::1]:8767"


class TestAnIdIsSomethingAPersonTypes:
    """The ID is read off one screen and typed into another, so its LENGTH is a feature.

    The first version spelled every part out as text — fourteen characters for a four-byte
    address, thirty-two for a sixteen-byte key — and produced an eighty-four-character string
    for a machine on a mesh address. Nobody transcribes that correctly.
    """

    def test_a_v4_address_fits_in_forty_characters(self):
        code = format_pairing_code(
            PairingAddress("100.126.254.35", 8767, "a" * TOKEN_CHARS)
        )

        assert len(code.replace("-", "")) <= 40

    def test_a_v6_address_costs_more_but_still_round_trips(self):
        address = PairingAddress("fd7a:115c:a1e0::4839:fe24", 8767, "b" * TOKEN_CHARS)

        assert parse_pairing_code(format_pairing_code(address)) == address

    def test_a_hostname_round_trips_too(self):
        # Nothing produces one today — addresses come from the routing table — but an ID is a
        # FORMAT, and one that cannot carry a name could never be taught to.
        address = PairingAddress("my-mac.local", 8767, "c" * TOKEN_CHARS)

        assert parse_pairing_code(format_pairing_code(address)) == address

    def test_the_key_survives_the_packing_exactly(self):
        # It is compared byte-for-byte against the header a peer sends; one bit lost here and
        # nothing opens, with no sign of where it went.
        token = "0123456789abcdef" * 2
        address = PairingAddress("192.168.0.101", 8767, token)

        assert parse_pairing_code(format_pairing_code(address)).token == token

    def test_a_high_port_survives_the_two_bytes_it_is_packed_into(self):
        address = PairingAddress("10.0.0.2", 65535, "d" * TOKEN_CHARS)

        assert parse_pairing_code(format_pairing_code(address)).port == 65535

    def test_a_truncated_code_is_reported_as_truncated(self):
        code = format_pairing_code(PairingAddress("10.0.0.2", 8767, "e" * TOKEN_CHARS))

        with pytest.raises(PairingError, match="incomplete or was mistyped"):
            parse_pairing_code(code.replace("-", "")[:-8])
