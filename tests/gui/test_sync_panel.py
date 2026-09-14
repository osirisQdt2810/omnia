"""Tests for the Sync panel: what each state says, and what the socket's lifecycle does.

The panel's job is that every state it can be in gets WORDS. A sync that cannot start is the
normal case while somebody sets this up on two machines, and "could not connect" with nothing
after it is the answer that leaves them with nothing to try — so most of these tests are about
what is written on the page rather than about what it computes.
"""

from __future__ import annotations

from omnia.core.sync import Inventory, MachineIdentity
from omnia.gui.sync import session
from omnia.gui.sync.collect import inventory_reader
from omnia.gui.sync.html import PanelState, build_sync_html


class _Repo:
    """A config repository stand-in that remembers what was written, by section."""

    def __init__(self, **sections: dict) -> None:
        self.sections: dict[str, dict] = dict(sections)

    def raw_section(self, section: str) -> dict:
        return dict(self.sections.get(section, {}))

    def update_section(self, section: str, values: dict) -> None:
        self.sections.setdefault(section, {}).update(values)


def _page(state: PanelState) -> str:
    return build_sync_html(state, dark=False)


class TestWhatThisMachineSays:
    def test_sharing_off_says_what_turning_it_on_is_for(self):
        page = _page(PanelState(sharing=False))

        assert "copy from this computer to another one" in page
        assert 'id="sync-sharing">' in page  # the switch is drawn off

    def test_sharing_on_shows_the_two_numbers_to_read_out(self):
        page = _page(
            PanelState(
                sharing=True, machine_id="168 604 419 56", access_code="123 456 789"
            )
        )

        assert "168 604 419 56" in page
        assert "123 456 789" in page
        assert "Type both of these into the other computer" in page
        assert 'id="sync-sharing" checked>' in page

    def test_each_number_has_its_own_copy_button(self):
        # One handler, two buttons, each naming what it copies — a single Copy button beside two
        # numbers is a button whose meaning the reader has to guess.
        page = _page(
            PanelState(sharing=True, machine_id="168 604 419 56", access_code="1 2 3")
        )

        assert 'data-copy="sync-id"' in page
        assert 'data-copy="sync-code"' in page

    def test_sharing_on_with_no_address_says_so_instead_of_showing_nothing(self):
        # The distinction that matters: the ID exists, it is simply not reachable. Drawing an
        # empty box here would read as "something is broken" rather than "connect to a network".
        page = _page(PanelState(sharing=True, machine_id=""))

        assert "no address another one could reach" in page
        assert 'id="sync-id"' not in page

    def test_the_page_never_mentions_the_transport(self):
        # The user's model is two machines and an ID. Naming a VPN, a port or an IP here would
        # make the panel a thing to configure rather than a thing to use.
        page = _page(
            PanelState(
                sharing=True, machine_id="168 604 419 56", access_code="123 456 789"
            )
        )

        lowered = page.lower()
        for word in ("tailscale", "vpn", "ip address", "port ", "http"):
            assert word not in lowered


class TestWhatTheOtherMachineSays:
    def test_a_failure_is_rendered_as_a_warning_not_a_success(self):
        page = _page(
            PanelState(status="That ID does not open that machine.", connected=False)
        )

        assert "sync-warn" in page
        assert "does not open that machine" in page

    def test_what_the_other_machine_holds_is_not_listed_here(self):
        # It opens in its own window. A panel that also listed a thousand decks would scroll the
        # ID and the access code away exactly when somebody is reading them out.
        page = _page(PanelState(status="Connected to mac-mini.", connected=True))

        assert "sync-decks" not in page
        assert "Connected to mac-mini." in page

    def test_both_typed_numbers_survive_a_failed_check(self):
        # Retyping twenty digits by hand after a typo in one of them is the part of this feature
        # people give up on.
        page = _page(
            PanelState(
                peer_id="168 604 419 56",
                peer_code="123 456 789",
                status="Could not connect.",
            )
        )

        assert 'value="168 604 419 56"' in page
        assert 'value="123 456 789"' in page


class TestTheSharingSession:
    def _identity(self, **kwargs) -> MachineIdentity:
        values = {"key": "k" * 32, "port": 0, "sharing": True}
        values.update(kwargs)
        return MachineIdentity(**values)

    def teardown_method(self) -> None:
        session.stop()

    @staticmethod
    def _free_port() -> int:
        """A port nothing holds right now.

        Not the default: `_port()` reads 0 as "unset" and answers 8767, so a stored port of 0
        makes this bind the REAL sharing port — which a developer running Anki with sharing on
        already holds, and the test fails for a reason that has nothing to do with the code.
        """
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def test_starting_serves_and_stopping_closes(self):
        assert session.running() is False

        assert session.start(self._identity(), lambda: Inventory()) is True
        assert session.running() is True

        session.stop()
        assert session.running() is False

    def test_starting_again_replaces_the_socket_rather_than_stacking_one(self):
        # The key and the port are what the socket was opened WITH, so a regenerated key has to
        # become a new socket — otherwise the old ID keeps opening this machine.
        session.start(self._identity(key="a" * 32), lambda: Inventory())
        session.start(self._identity(key="b" * 32), lambda: Inventory())

        assert session.running() is True
        session.stop()
        assert session.running() is False

    def test_a_session_with_no_key_refuses_to_serve(self):
        assert session.start(self._identity(key=""), lambda: Inventory()) is False
        assert session.running() is False

    def test_sharing_left_off_is_not_restored_at_profile_open(self):
        repo = _Repo(
            sync={"key": "k" * 32, "sharing": False, "port": self._free_port()}
        )

        assert session.start_if_enabled(repo, lambda: Inventory()) is False
        assert session.running() is False

    def test_sharing_left_on_is_restored_at_profile_open(self):
        # The switch is a preference, not a session: turning it on did not mean "until I quit".
        repo = _Repo(sync={"key": "k" * 32, "sharing": True, "port": self._free_port()})

        assert session.start_if_enabled(repo, lambda: Inventory()) is True
        assert session.running() is True

    def test_a_port_that_is_taken_reports_rather_than_raising(self):
        # A profile must load even when something else owns the port — a second copy of Anki, or
        # an unrelated program. It has to come back as False, never as a traceback during
        # startup; the panel is where the user finds out.
        import socket

        # The wildcard, which is what a session binds: on BSD/macOS SO_REUSEADDR lets a wildcard
        # bind sit beside a 127.0.0.1 one, so a loopback blocker would not be the conflict a
        # second copy of Anki actually is.
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        try:
            taken = _Repo(
                sync={
                    "key": "k" * 32,
                    "sharing": True,
                    "port": blocker.getsockname()[1],
                }
            )

            assert session.start_if_enabled(taken, lambda: Inventory()) is False
            assert session.running() is False
        finally:
            blocker.close()


class TestWhatThisMachineOffers:
    def test_the_reader_is_built_once_and_called_later(self):
        # It is called on the session's WORKER thread, so building it must touch no collection.
        read = inventory_reader(_Repo())

        assert callable(read)
