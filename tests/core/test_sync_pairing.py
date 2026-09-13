"""Tests for a machine's ID and the address ranking folded into it.

The user handles exactly one thing — an ID per machine — so both halves here exist to stop the
same failure shape: an ID that looks right and simply never connects. There is no rendezvous
server (ADR-020), so the ID must carry the address, which means one minted from an address
nothing can dial is a dead end with nothing on screen to fix. Hence the ranking refuses to offer
loopback at all, and the parser forgives how an ID travels (dashes, spaces, case) while refusing
outright anything it cannot turn back into an address.
"""

from __future__ import annotations

import pytest

from omnia.core.sync import (
    KIND_LAN,
    KIND_MESH,
    KIND_OTHER,
    PairingAddress,
    PairingError,
    format_pairing_code,
    new_token,
    parse_pairing_code,
    rank_addresses,
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


class TestRankingWhatToPublish:
    def test_a_mesh_address_beats_a_lan_one(self):
        # The mesh address is the only kind that works when the two machines are not on the
        # same network — which is the case this feature exists for.
        ranked = rank_addresses(["192.168.1.40", "100.71.161.7"])

        assert [address.ip for address in ranked] == ["100.71.161.7", "192.168.1.40"]
        assert ranked[0].kind == KIND_MESH
        assert ranked[1].kind == KIND_LAN

    def test_loopback_is_not_offered_at_all(self):
        # Ranked last it would still be offered, and a code made from it can never connect:
        # nothing to read on screen, nothing to fix.
        ranked = rank_addresses(["127.0.0.1", "::1", "192.168.1.40"])

        assert [address.ip for address in ranked] == ["192.168.1.40"]

    def test_link_local_is_not_offered_either(self):
        ranked = rank_addresses(["169.254.10.1", "fe80::1", "10.0.0.5"])

        assert [address.ip for address in ranked] == ["10.0.0.5"]

    def test_a_public_address_ranks_last_but_is_kept(self):
        ranked = rank_addresses(["8.8.8.8", "10.0.0.5"])

        assert [address.ip for address in ranked] == ["10.0.0.5", "8.8.8.8"]
        assert ranked[-1].kind == KIND_OTHER

    def test_duplicates_and_junk_are_dropped(self):
        ranked = rank_addresses(["10.0.0.5", "10.0.0.5", "", "not-an-address", None])

        assert [address.ip for address in ranked] == ["10.0.0.5"]

    def test_nothing_usable_is_an_empty_answer_not_an_error(self):
        # A machine with only loopback has nothing to publish, and the session has to render
        # that state rather than crash on it.
        assert rank_addresses(["127.0.0.1"]) == []

    def test_no_product_name_appears_even_in_the_log_labels(self):
        # These are not rendered anywhere — the user handles an ID and nothing else. The rule is
        # pinned here anyway: the day one of these strings is copied into a dialog by some other
        # change, it must still say nothing about the tool underneath.
        for kind in (KIND_MESH, KIND_LAN, KIND_OTHER):
            lowered = kind.lower()
            assert "tailscale" not in lowered
            assert "vpn" not in lowered
            assert "mesh" not in lowered
