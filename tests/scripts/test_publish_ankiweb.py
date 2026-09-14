"""Tests for the AnkiWeb upload: the wire format, and everything that must not leak or lie.

The encoder is hand-written because the endpoint speaks protobuf and a dependency would buy
nothing that sixty lines do not. Hand-written means the wire format needs pinning — a field
number off by one produces bytes the server rejects with a 400 and an empty body, which is
indistinguishable from every other rejection and has already cost hours once.

The rest is about a credential in a PUBLIC repository: the session must never reach a command
line, a log, or an error message, and an upload that would blank the listing's description must
stop rather than proceed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import publish_ankiweb as publish  # noqa: E402


def _fields(payload: bytes) -> dict[int, object]:
    """Decode a flat protobuf message into ``{field number: value}``.

    Varints come back as ints and length-delimited fields as bytes — enough to assert that the
    encoder put each value under the number the schema says, which is the whole risk here.
    """
    out: dict[int, object] = {}
    index = 0
    while index < len(payload):
        key, index = publish._read_varint(payload, index)
        field, wire = key >> 3, key & 0x07
        if wire == 0:
            value, index = publish._read_varint(payload, index)
            out[field] = value
        elif wire == 2:
            length, index = publish._read_varint(payload, index)
            out[field] = payload[index : index + length]
            index += length
        else:
            raise AssertionError(f"unexpected wire type {wire}")
    return out


class TestTheWireFormat:
    def test_a_varint_round_trips(self):
        for value in (0, 1, 127, 128, 300, 250900, 726991726, 2**31):
            assert publish._read_varint(publish._varint(value), 0)[0] == value

    def test_a_negative_number_is_refused_rather_than_wrapped(self):
        # int32 on the wire encodes negatives as ten bytes; silently emitting the wrong thing
        # would be a 400 with no reason attached.
        with pytest.raises(publish.PublishError, match="negative"):
            publish._varint(-1)

    def test_addon_info_puts_each_value_under_its_schema_number(self):
        info = _fields(
            publish.addon_info(
                addon_id=726991726,
                title="Omnia",
                tags="",
                support_url="https://example.test",
                description="hello",
            )
        )

        assert info[1] == 726991726  # addon_id — what makes this an UPDATE
        assert info[2] == b"Omnia"
        assert info[3] == b""
        assert info[4] == b"https://example.test"
        assert info[5] == b"hello"
        assert _fields(info[6])[1] == publish.MIN_POINT_VERSION

    def test_the_version_branch_months_are_real_months(self):
        # 26.99 is month 99. The form clamps it, the server rejects the upload with a 400 and an
        # empty body, and nothing on screen says why. Checked on the MAGNITUDE, because the
        # maximum is negative on purpose — see TestTheVersionBranch.
        for point in (publish.MIN_POINT_VERSION, publish.MAX_POINT_VERSION):
            if not point:
                continue
            month = abs(point) // 100 % 100
            assert 1 <= month <= 12, point

    def test_an_empty_string_field_is_still_sent(self):
        # proto3 cannot tell "unset" from "empty", and the server takes what it is given: a
        # description omitted is a description blanked.
        info = _fields(
            publish.addon_info(
                addon_id=1, title="t", tags="", support_url="", description=""
            )
        )

        assert info[3] == b"" and info[4] == b"" and info[5] == b""

    def test_the_package_travels_under_field_one_of_the_file(self):
        file = _fields(publish.addon_file(b"PK\x03\x04zip", branch_index=0))

        assert file[1] == b"PK\x03\x04zip"
        assert 2 not in file  # branch 0 is proto3's default and is not put on the wire

    def test_the_request_nests_info_then_file(self):
        request = _fields(publish.upload_request(b"\x08\x01", b"\x0a\x03abc"))

        assert request[1] == b"\x08\x01"
        assert request[2] == b"\x0a\x03abc"

    def test_the_answer_gives_up_the_addon_id(self):
        assert publish.read_addon_id(publish._uint(1, 726991726)) == 726991726

    def test_an_answer_without_an_id_reads_as_zero(self):
        # Treated by the caller as "it answered, but not with an id" — not as success.
        assert publish.read_addon_id(b"") == 0
        assert publish.read_addon_id(publish._text(2, "something else")) == 0


class TestWhatMustNotHappen:
    def test_the_description_is_required(self, monkeypatch, tmp_path):
        # Uploading with an empty description would overwrite the listing page with nothing.
        monkeypatch.setattr(publish, "DESCRIPTION_FILE", tmp_path / "absent.md")

        with pytest.raises(publish.PublishError, match="blank the page"):
            publish.description()

    def test_an_empty_description_file_is_refused_too(self, monkeypatch, tmp_path):
        empty = tmp_path / "ankiweb.md"
        empty.write_text("   \n", encoding="utf-8")
        monkeypatch.setattr(publish, "DESCRIPTION_FILE", empty)

        with pytest.raises(publish.PublishError, match="empty"):
            publish.description()

    def test_the_shipped_description_exists_and_says_what_it_is(self):
        # It is the listing's page. If this file ever goes missing the release stops, which is
        # the point — but it going missing silently in a refactor is what this catches.
        text = publish.description()

        assert "Omnia" in text
        assert len(text) > 200

    def test_a_missing_package_stops_before_any_request(self, tmp_path):
        with pytest.raises(publish.PublishError, match="no package"):
            publish.publish(tmp_path / "nothing.ankiaddon", "v0.1.0", "cookie")

    def test_no_cookie_means_no_upload(self, monkeypatch, capsys):
        monkeypatch.delenv("OMNIA_ANKIWEB_COOKIE", raising=False)

        code = publish.main(["publish.py", "dist/omnia.ankiaddon", "v0.1.0"])

        assert code == 1
        assert "not set" in capsys.readouterr().err

    def test_a_refused_session_says_how_to_fix_it(self):
        for status in (401, 403):
            message = publish._explain(status)
            assert "ANKIWEB_COOKIE" in message and "log" in message.lower()

    def test_the_empty_400_names_its_usual_cause(self):
        # The server sends no reason at all, so this is the only place the cause can be said.
        assert "month" in publish._explain(400)

    def test_a_failure_never_carries_the_response_body(self, monkeypatch, tmp_path):
        # A body can echo a request header — the session among them — back into a log this
        # repository publishes to the world. So the error is built from the status code alone.
        import io
        import urllib.error

        package = tmp_path / "omnia.ankiaddon"
        package.write_bytes(b"PK\x03\x04")

        def _raise(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                publish.UPLOAD_URL,
                400,
                "Bad Request",
                {},
                io.BytesIO(b"echoed: ankiweb=SUPER-SECRET-SESSION"),
            )

        monkeypatch.setattr(publish.urllib.request, "urlopen", _raise)

        with pytest.raises(publish.PublishError) as caught:
            publish.publish(package, "v0.1.0", "SUPER-SECRET-SESSION")

        assert "SUPER-SECRET-SESSION" not in str(caught.value)
        assert "echoed" not in str(caught.value)

    def test_the_session_never_reaches_a_command_line(self):
        # argv shows up in logs and in `ps`. The script reads the environment instead, and this
        # pins that there is no other way in.
        source = Path(publish.__file__).read_text(encoding="utf-8")

        assert "OMNIA_ANKIWEB_COOKIE" in source
        assert (
            "sys.argv" in source
        )  # only the package path and the version arrive that way
        assert "--cookie" not in source and "argv[3]" not in source

    def test_the_session_goes_out_as_a_header_and_nowhere_else(
        self, monkeypatch, tmp_path
    ):
        package = tmp_path / "omnia.ankiaddon"
        package.write_bytes(b"PK\x03\x04")
        seen = {}

        class _Response:
            def read(self):
                return publish._uint(1, 726991726)

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def _capture(request, timeout=None):
            seen["headers"] = dict(request.headers)
            seen["body"] = request.data
            return _Response()

        monkeypatch.setattr(publish.urllib.request, "urlopen", _capture)

        assert publish.publish(package, "v0.1.0", "SECRET") == 726991726
        assert seen["headers"]["Cookie"] == "has_auth=1; ankiweb=SECRET"
        assert b"SECRET" not in seen["body"]  # never in the payload, only in the header


class TestTheVersionBranch:
    """The branch is where this has now failed twice, both times with a bare 400.

    Once on an impossible month, once on a maximum of zero — which proto3 drops from the wire
    entirely, so the request carried no maximum at all. The server says nothing either time, so
    the only defence is to check the bytes here.
    """

    @staticmethod
    def _fields(raw: bytes) -> dict:
        """Decode a branch message the way the server's reader would."""
        out, index = {}, 0
        while index < len(raw):
            key, index = publish._read_varint(raw, index)
            value, index = publish._read_varint(raw, index)
            signed = value - (1 << 64) if value >= (1 << 63) else value
            out[key >> 3] = signed
        return out

    def test_the_minimum_is_a_real_month(self):
        # A month over 12 is meaningless, the upload form clamps it silently, and the server
        # rejects the whole request with an empty body.
        month = (publish.MIN_POINT_VERSION // 100) % 100
        assert 1 <= month <= 12, f"{publish.MIN_POINT_VERSION} has month {month}"

    def test_the_maximum_is_a_real_month_too(self):
        month = (abs(publish.MAX_POINT_VERSION) // 100) % 100
        assert 1 <= month <= 12

    def test_the_maximum_is_negative_which_is_how_ankiweb_spells_open_ended(self):
        # Read out of the upload page's own code: max_version is a signed int32, the page renders
        # a negative with a leading minus, and "Add New Branch" flips the previous branch's
        # negative max positive before opening the next — which only makes sense if negative is
        # the open-ended one. A POSITIVE ceiling is an exact version, so users on the next Anki
        # release silently stop getting updates.
        assert publish.MAX_POINT_VERSION < 0

    def test_the_maximum_actually_reaches_the_wire(self):
        # The defect this closes: zero is proto3's default, so the field was dropped entirely and
        # the request carried no maximum. Asserted on the BYTES, because that is the only place
        # the difference between "0" and "absent" shows up.
        fields = self._fields(
            publish.addon_branch(publish.MIN_POINT_VERSION, publish.MAX_POINT_VERSION)
        )

        assert 2 in fields, "the branch went out with no maximum at all"
        assert fields[2] == publish.MAX_POINT_VERSION

    def test_a_negative_survives_as_a_negative(self):
        # int32 is two's complement on the wire, not zig-zag. Encode it as zig-zag or as an
        # unsigned varint and the server reads a wildly large positive version instead.
        fields = self._fields(publish.addon_branch(250900, -260900))

        assert fields[1] == 250900
        assert fields[2] == -260900

    def test_a_zero_maximum_would_vanish_which_is_why_it_is_not_used(self):
        # Pins the mechanism rather than the value, so the next person to reach for 0 sees why.
        assert 2 not in self._fields(publish.addon_branch(250900, 0))
