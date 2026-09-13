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
    KIND_ULA,
    KIND_VIRTUAL,
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

    def test_a_public_address_ranks_below_a_lan_one_but_is_kept(self):
        ranked = rank_addresses(["8.8.8.8", "10.0.0.5"])

        assert [address.ip for address in ranked] == ["10.0.0.5", "8.8.8.8"]
        assert ranked[-1].kind == KIND_OTHER

    def test_a_container_bridge_loses_to_the_real_network(self):
        # The failure this ordering exists for: a machine with Docker installed carries
        # 172.17.0.1 on docker0, which belongs to a network living inside that machine. Sorted
        # by address TEXT it comes first, and the ID then carries an address no machine on earth
        # can dial — a connect timeout with nothing on screen to fix.
        ranked = rank_addresses(["172.17.0.1", "192.168.1.40"])

        assert [address.ip for address in ranked] == ["192.168.1.40", "172.17.0.1"]
        assert ranked[-1].kind == KIND_VIRTUAL

    @pytest.mark.parametrize(
        "virtual", ["172.17.0.1", "192.168.56.1", "10.211.55.2", "192.168.64.1"]
    )
    def test_every_known_host_only_range_ranks_last(self, virtual):
        ranked = rank_addresses([virtual, "192.168.1.40"])

        assert ranked[0].ip == "192.168.1.40"

    def test_a_virtual_address_is_still_offered_when_it_is_all_there_is(self):
        # Ranked last, not dropped: unlike loopback it is not IMPOSSIBLE for one of these to be
        # somebody's real LAN, and ranked last is enough when the caller takes the first.
        ranked = rank_addresses(["172.17.0.1"])

        assert [address.ip for address in ranked] == ["172.17.0.1"]

    def test_a_mesh_v6_address_beats_a_lan_v4_one(self):
        # The v6 side of the mesh is a unique-local address, so without recognising its prefix
        # it lands in "private" alongside the LAN — and then loses the tie-break, inverting the
        # one ranking rule that matters.
        ranked = rank_addresses(["192.168.1.40", "fd7a:115c:a1e0::1"])

        assert ranked[0].ip == "fd7a:115c:a1e0::1"
        assert ranked[0].kind == KIND_MESH

    def test_another_unique_local_v6_sits_between_mesh_and_lan(self):
        ranked = rank_addresses(["192.168.1.40", "fd00:abcd::1", "100.71.161.7"])

        assert [address.ip for address in ranked] == [
            "100.71.161.7",
            "fd00:abcd::1",
            "192.168.1.40",
        ]
        assert ranked[1].kind == KIND_ULA

    def test_equal_ranks_keep_the_order_they_were_found_in(self):
        # Discovery order carries information (the probes are ordered by preference); the
        # address text carries none, and sorting by it is what put a container bridge first.
        ranked = rank_addresses(["192.168.1.40", "10.0.0.5"])

        assert [address.ip for address in ranked] == ["192.168.1.40", "10.0.0.5"]

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
        for kind in (KIND_MESH, KIND_ULA, KIND_LAN, KIND_OTHER, KIND_VIRTUAL):
            lowered = kind.lower()
            assert "tailscale" not in lowered
            assert "vpn" not in lowered
            assert "mesh" not in lowered


class TestFindingThisMachinesAddresses:
    """``local_addresses`` asks the routing table, not the name resolver.

    ``getaddrinfo(gethostname())`` answers "what does this machine's NAME point at" — on macOS
    whatever mDNS advertises, which does not include a VPN interface, i.e. exactly the address
    class this feature exists to use. A UDP connect sends no packet; it makes the kernel choose
    a route and then reports the source address that route would use.
    """

    @staticmethod
    def _routed(routes):
        """A fake socket whose connect() succeeds only for targets in ``routes``."""

        class _Socket:
            def __init__(self, family, _type):
                self.family = family
                self.name = None

            def connect(self, endpoint):
                host = endpoint[0]
                if host not in routes:
                    raise OSError("no route to host")
                self.name = routes[host]

            def getsockname(self):
                return (self.name, 9)

            def close(self):
                pass

        return _Socket

    def test_the_mesh_route_is_reported_first(self, monkeypatch):
        from omnia.core.sync import reachability

        monkeypatch.setattr(
            reachability.socket,
            "socket",
            self._routed(
                {"100.100.100.100": "100.126.254.35", "8.8.8.8": "192.168.0.101"}
            ),
        )

        assert reachability.local_addresses() == ["100.126.254.35", "192.168.0.101"]

    def test_a_machine_with_no_mesh_still_reports_its_default_route(self, monkeypatch):
        from omnia.core.sync import reachability

        monkeypatch.setattr(
            reachability.socket, "socket", self._routed({"8.8.8.8": "192.168.0.101"})
        )

        assert reachability.local_addresses() == ["192.168.0.101"]

    def test_the_same_address_on_two_probes_is_reported_once(self, monkeypatch):
        from omnia.core.sync import reachability

        monkeypatch.setattr(
            reachability.socket,
            "socket",
            self._routed({"100.100.100.100": "10.0.0.5", "8.8.8.8": "10.0.0.5"}),
        )

        assert reachability.local_addresses() == ["10.0.0.5"]

    def test_no_route_at_all_is_an_empty_list(self, monkeypatch):
        # An offline machine has nothing to publish, and the caller renders that rather than
        # crashing on it.
        from omnia.core.sync import reachability

        monkeypatch.setattr(reachability.socket, "socket", self._routed({}))

        assert reachability.local_addresses() == []

    def test_no_name_lookup_happens(self, monkeypatch):
        # The point of the whole approach: resolving the host name is the call that answers the
        # wrong question, so it must not be on this path at all.
        from omnia.core.sync import reachability

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("local_addresses must not resolve names")

        monkeypatch.setattr(reachability.socket, "getaddrinfo", _forbidden)
        monkeypatch.setattr(reachability.socket, "gethostname", _forbidden)
        monkeypatch.setattr(
            reachability.socket, "socket", self._routed({"8.8.8.8": "192.168.0.101"})
        )

        assert reachability.local_addresses() == ["192.168.0.101"]
