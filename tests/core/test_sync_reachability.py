"""Tests for which of this machine's addresses another machine could dial.

One failure shape runs through all of it: an ID minted from an address nothing can reach looks
perfectly fine and simply never connects, leaving a connect timeout with nothing on screen to
fix. So the ranking refuses to offer loopback at all, puts the ranges that exist only inside
this machine last, and finds candidates by asking the routing table rather than the name
resolver — which answers a different question and misses the one address class this feature
exists to use.
"""

from __future__ import annotations

import socket

import pytest

from omnia.core.sync import (
    KIND_LAN,
    KIND_MESH,
    KIND_OTHER,
    KIND_ULA,
    KIND_VIRTUAL,
    rank_addresses,
    reachability,
)


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

    def test_a_real_lan_on_172_16_is_not_mistaken_for_a_bridge(self):
        # Docker's pool starts at 172.17, so 172.16 is somebody's real network. Spelling the
        # rule as 172.16.0.0/12 would have been tidier and would have demoted it.
        ranked = rank_addresses(["172.16.4.9"])

        assert ranked[0].kind == KIND_LAN

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
        "virtual",
        [
            "172.17.0.1",  # docker0
            "172.20.0.5",  # a later docker pool — the span runs to 172.31
            "172.31.0.9",
            "192.168.56.1",  # VirtualBox host-only
            "10.211.55.2",  # Parallels
            "192.168.64.1",  # the macOS virtualisation framework
        ],
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
    def _routed(routes, *, families=None):
        """A fake socket factory: ``routes`` decides which targets have a route.

        ``families`` limits which address families can be CONSTRUCTED at all — that is how a
        machine with IPv6 unbound behaves, and it is a different failure from "no route": it
        raises from ``socket()`` before there is anything to connect.
        """

        class _Socket:
            def __init__(self, family, _type):
                if families is not None and family not in families:
                    raise OSError(97, "Address family not supported by protocol")
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
        monkeypatch.setattr(
            reachability,
            "_socket_factory",
            self._routed(
                {"100.100.100.100": "100.126.254.35", "8.8.8.8": "192.168.0.101"}
            ),
        )

        assert reachability.local_addresses() == ["100.126.254.35", "192.168.0.101"]

    def test_a_machine_with_no_mesh_still_reports_its_default_route(self, monkeypatch):
        monkeypatch.setattr(
            reachability, "_socket_factory", self._routed({"8.8.8.8": "192.168.0.101"})
        )

        assert reachability.local_addresses() == ["192.168.0.101"]

    def test_the_same_address_on_two_probes_is_reported_once(self, monkeypatch):
        monkeypatch.setattr(
            reachability,
            "_socket_factory",
            self._routed({"100.100.100.100": "10.0.0.5", "8.8.8.8": "10.0.0.5"}),
        )

        assert reachability.local_addresses() == ["10.0.0.5"]

    def test_no_route_at_all_is_an_empty_list(self, monkeypatch):
        # An offline machine has nothing to publish, and the caller renders that rather than
        # crashing on it.
        monkeypatch.setattr(reachability, "_socket_factory", self._routed({}))

        assert reachability.local_addresses() == []

    def test_a_machine_with_ipv6_unbound_still_answers(self, monkeypatch):
        # Windows with the IPv6 box unticked, or Linux booted ipv6.disable=1: socket() itself
        # raises EAFNOSUPPORT before there is anything to connect. With the construction left
        # outside the guard that exception escaped past the IPv4 answer already found, and
        # reached a dialog whose whole job was to print an address the machine plainly had.
        monkeypatch.setattr(
            reachability,
            "_socket_factory",
            self._routed(
                {"100.100.100.100": "100.126.254.35", "8.8.8.8": "192.168.0.101"},
                families={socket.AF_INET},
            ),
        )

        assert reachability.local_addresses() == ["100.126.254.35", "192.168.0.101"]

    def test_no_name_lookup_happens(self, monkeypatch):
        # The point of the whole approach: resolving the host name is the call that answers the
        # wrong question, so it must not be on this path at all.
        def _forbidden(*_args, **_kwargs):
            raise AssertionError("local_addresses must not resolve names")

        monkeypatch.setattr(reachability.socket, "getaddrinfo", _forbidden)
        monkeypatch.setattr(reachability.socket, "gethostname", _forbidden)
        monkeypatch.setattr(
            reachability, "_socket_factory", self._routed({"8.8.8.8": "192.168.0.101"})
        )

        assert reachability.local_addresses() == ["192.168.0.101"]
