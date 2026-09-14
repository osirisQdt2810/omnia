"""Tests for what a user actually handles: a machine's ID, and the code that opens it.

There is no rendezvous server (ADR-020). Remote-desktop tools get away with nine digits because
they run infrastructure that turns a number into an address; nothing here does, so the ID must BE
the address — which uses every digit it has and forces the access code into a second field.

Two properties carry the rest, and both are checked exhaustively rather than argued for:

* an ID that was mistyped says so, instead of naming a different machine that was never asked;
* a code is short enough to read aloud, which is only safe because the service refuses to be
  guessed at — that half is pinned next door, in the session tests.
"""

from __future__ import annotations

import random

import pytest

from omnia.core.sync import (
    DEFAULT_PORT,
    ID_DIGITS,
    PASSCODE_DIGITS,
    PairingAddress,
    PairingError,
    format_machine_id,
    format_passcode,
    new_token,
    parse_machine_id,
    parse_passcode,
)


def _digits(code: str) -> str:
    return code.replace(" ", "").replace("-", "")


class TestTheIdIsANumberAPersonCanType:
    def test_an_ordinary_machine_gets_eleven_digits(self):
        # The whole point of the redesign: ten digits of address, one to catch a typo.
        assert len(_digits(format_machine_id("100.126.254.35"))) == ID_DIGITS

    def test_it_round_trips(self):
        assert parse_machine_id(format_machine_id("192.168.0.101")) == (
            "192.168.0.101",
            DEFAULT_PORT,
        )

    def test_a_moved_port_is_carried_and_costs_digits(self):
        # A machine whose owner moved the port has already left the ordinary case; spelling the
        # port out is better than a shorter ID that silently drops it.
        code = format_machine_id("10.0.0.2", 9000)

        assert parse_machine_id(code) == ("10.0.0.2", 9000)
        assert len(_digits(code)) > ID_DIGITS

    def test_a_v6_address_round_trips_too(self):
        address = "fd7a:115c:a1e0::4839:fe24"

        assert parse_machine_id(format_machine_id(address)) == (address, DEFAULT_PORT)

    def test_it_is_grouped_for_reading_aloud(self):
        assert " " in format_machine_id("100.126.254.35")

    def test_how_it_arrives_does_not_matter(self):
        code = format_machine_id("100.126.254.35")
        expected = parse_machine_id(code)

        for mangled in (
            _digits(code),
            code.replace(" ", "-"),
            f"  {code}\n",
            code.replace(" ", "."),
        ):
            assert parse_machine_id(mangled) == expected

    def test_a_name_cannot_be_an_id(self):
        # Nothing produces one — addresses come from the routing table — and saying so is better
        # than an ID that cannot be a number pretending to be one.
        with pytest.raises(PairingError):
            format_machine_id("my-mac.local")

    def test_letters_are_refused_as_not_an_id(self):
        with pytest.raises(PairingError, match="only digits"):
            parse_machine_id("ABCDE-FGHIJ")

    def test_nothing_typed_says_so(self):
        with pytest.raises(PairingError, match="no ID"):
            parse_machine_id("   ")


class TestAMistypedIdSaysSo:
    """Without this, one wrong digit is a DIFFERENT valid address.

    The user would then spend the evening on "could not connect" for a machine that was never
    asked, which is the single most expensive way this feature could fail.
    """

    @staticmethod
    def _ids(count: int = 300) -> list[str]:
        rnd = random.Random(20260914)
        return [
            _digits(
                format_machine_id(".".join(str(rnd.randrange(256)) for _ in range(4)))
            )
            for _ in range(count)
        ]

    def test_every_single_wrong_digit_is_caught(self):
        missed = []
        for code in self._ids():
            for index in range(len(code)):
                for digit in "0123456789":
                    if digit == code[index]:
                        continue
                    typo = code[:index] + digit + code[index + 1 :]
                    try:
                        parse_machine_id(typo)
                    except PairingError:
                        continue
                    missed.append((code, typo))

        assert not missed, f"{len(missed)} single-digit typos read as a valid ID"

    def test_every_pair_of_swapped_neighbours_is_caught(self):
        # The second most common way to mistype a number you are copying, and the harder one to
        # spot by eye — every digit is still there.
        missed = []
        for code in self._ids():
            for index in range(len(code) - 1):
                if code[index] == code[index + 1]:
                    continue
                swapped = (
                    code[:index] + code[index + 1] + code[index] + code[index + 2 :]
                )
                try:
                    parse_machine_id(swapped)
                except PairingError:
                    continue
                missed.append((code, swapped))

        assert not missed, f"{len(missed)} swapped pairs read as a valid ID"

    def test_the_message_says_it_was_mistyped_rather_than_something_vaguer(self):
        code = _digits(format_machine_id("100.126.254.35"))
        typo = code[:-1] + str((int(code[-1]) + 1) % 10)

        with pytest.raises(PairingError, match="mistyped"):
            parse_machine_id(typo)

    def test_a_valid_number_of_a_length_nothing_produces_names_the_other_build(self):
        # Distinct from "mistyped": the check digit is RIGHT, so re-reading the ID will not help
        # and saying so would send the user round in circles. The one digit that satisfies the
        # check is found by trying all ten — the check digit itself is private, and pinning its
        # value here would be pinning the algorithm rather than the behaviour.
        messages = set()
        for digit in "0123456789":
            try:
                parse_machine_id("1" * 20 + digit)
            except PairingError as exc:
                messages.add(str(exc))

        assert any("different version" in message for message in messages)


class TestTheAccessCode:
    def test_it_is_nine_digits(self):
        assert len(new_token()) == PASSCODE_DIGITS
        assert new_token().isdigit()

    def test_it_is_grouped_for_reading_aloud_and_read_back(self):
        token = new_token()

        assert parse_passcode(format_passcode(token)) == token

    def test_how_it_arrives_does_not_matter(self):
        token = new_token()

        for mangled in (token, format_passcode(token), f" {token} ", "-".join(token)):
            assert parse_passcode(mangled) == token

    def test_a_wrong_length_says_how_long_it_should_be(self):
        with pytest.raises(PairingError, match=str(PASSCODE_DIGITS)):
            parse_passcode("123")

    def test_nothing_typed_says_so(self):
        with pytest.raises(PairingError, match="no access code"):
            parse_passcode("")

    def test_two_codes_are_not_the_same(self):
        assert len({new_token() for _ in range(200)}) > 190

    def test_it_is_not_predictable_from_the_clock(self):
        # `secrets`, not `random`: a code minted from a seeded PRNG is one somebody can compute
        # rather than guess, and no lockout helps against that.
        import inspect

        from omnia.core.sync import pairing

        assert "secrets." in inspect.getsource(pairing.new_token)


class TestAnAddressThatCannotWork:
    def test_a_pairing_address_refuses_a_bad_host(self):
        with pytest.raises(PairingError):
            PairingAddress("not an address", DEFAULT_PORT, new_token())

    def test_it_refuses_a_bad_port(self):
        with pytest.raises(PairingError):
            PairingAddress("10.0.0.2", 0, new_token())

    def test_it_refuses_something_that_is_not_an_access_code(self):
        with pytest.raises(PairingError, match="access code"):
            PairingAddress("10.0.0.2", DEFAULT_PORT, "abc")

    def test_a_v6_host_is_bracketed_in_the_url(self):
        address = PairingAddress("fd7a:115c:a1e0::1", DEFAULT_PORT, new_token())

        assert address.base_url == f"http://[fd7a:115c:a1e0::1]:{DEFAULT_PORT}"
