#!/usr/bin/env python3
"""Upload a built ``.ankiaddon`` to an existing AnkiWeb listing.

AnkiWeb has no public upload API. Its own documentation says to use the Upload button on the
website, and an Anki maintainer has stated on the official forum that the Terms do not currently
permit third-party API access — so this drives the private endpoint the site's own page calls,
with the owner's session, at the owner's explicit instruction. It is deliberately **not** wired
to every merge: it runs when a version tag is pushed, which is a person deciding to publish.

The endpoint speaks protobuf, not form-encoded fields::

    POST https://ankiweb.net/svc/shared/upload-addon
    Cookie: has_auth=1; ankiweb=<session>
    Content-Type: application/octet-stream

    UploadAddonRequest { AddonInfo info = 1; optional AddonFile file = 2; }
    AddonInfo  { optional uint32 addon_id = 1; string title = 2; string tags = 3;
                 string support_url = 4; string description = 5;
                 repeated AddonBranch branches = 6; }
    AddonBranch{ uint32 min_version = 1; int32 max_version = 2; }
    AddonFile  { bytes zip_data = 1; uint32 branch_index = 2; }
    -> UploadAddonResponse { uint32 addon_id = 1; }

Those four messages are encoded here by hand. Proto3's wire format for them is varints and
length-delimited bytes and nothing else, so a dependency would buy nothing that sixty lines do
not — and CI installing a package to send one request is a package that can break the release.

**The session is handled as a credential, in a PUBLIC repository whose action logs anyone can
read.** It is taken from the environment and never from a command line (argv shows up in logs
and in ``ps``), never echoed, never transformed — GitHub masks a secret by matching it exactly,
so base64-ing or splitting it defeats the masking — and a failure reports a status code and a
sentence rather than a response body that could echo a header back.

Usage:
    OMNIA_ANKIWEB_COOKIE=… python scripts/publish_ankiweb.py dist/omnia.ankiaddon v0.1.0
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from common import enable_utf8_output

UPLOAD_URL = "https://ankiweb.net/svc/shared/upload-addon"

#: The listing this publishes to. Sending it is what makes the upload an UPDATE rather than a
#: new add-on with a new code that nobody has installed.
ADDON_ID = 726991726
TITLE = "Omnia — All-in-One Toolkit"
#: The listing's search tags, declared here because this request REPLACES the whole record: an
#: empty string is not "leave them alone", it is "erase them", by the same mechanism that would
#: blank the description. Sent deliberately rather than defaulting to the value that destroys.
TAGS = "ai tts cloze automation reviewer"
SUPPORT_URL = "https://github.com/osirisQdt2810/omnia"

#: The oldest Anki this add-on supports, in AnkiWeb's scale — ``major*10000 + minor*100 + patch``,
#: so 25.09 is 250900. The MONTH half must be a real month: the upload form silently clamps an
#: impossible one and the server then rejects the whole request with a 400 and an EMPTY body,
#: which is indistinguishable from every other rejection and cost hours once already.
MIN_POINT_VERSION = 250900

#: The ceiling, NEGATIVE, which is how AnkiWeb spells "and everything newer".
#:
#: Read out of the upload page's own code rather than guessed. ``max_version`` is a SIGNED int32
#: (``{no:2,name:"max_version",kind:"scalar",T:5}``), the page renders a negative with a leading
#: minus and parses one back, and its "Add New Branch" button flips the previous branch's
#: negative max positive before opening the next — which only makes sense if a negative max is
#: the open-ended one.
#:
#: Zero does NOT mean "no maximum". Zero is proto3's default, so the field is dropped from the
#: wire entirely, and the server answers 400 with an empty body — the same silent rejection that
#: cost hours over an impossible month once before. A positive ceiling is worse than either: it
#: is an exact version, so ``260900`` blocks every Anki from 26.9.1 on, and users simply stop
#: getting updates with no signal anywhere.
MAX_POINT_VERSION = -260900

#: The listing's description, kept in the repo. AnkiWeb's request carries the description on
#: every upload, and proto3 cannot tell "unset" from "empty" — so omitting it would blank the
#: page. Keeping the text here means the listing is versioned with the code instead of living
#: only in a textarea nobody has a copy of.
DESCRIPTION_FILE = Path(__file__).resolve().parent.parent / "docs" / "ankiweb.md"


class PublishError(RuntimeError):
    """The upload did not happen, with a reason the log can show."""


# --- proto3 wire format -------------------------------------------------------------------


def _varint(value: int) -> bytes:
    """Encode a non-negative int as a base-128 varint."""
    if value < 0:
        raise PublishError(f"cannot encode a negative value ({value}) as a varint")
    out = bytearray()
    while True:
        seven = value & 0x7F
        value >>= 7
        out.append(seven | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    """The key byte(s) introducing ``field``."""
    return _varint((field << 3) | wire)


def _uint(field: int, value: int) -> bytes:
    """A varint field. Omitted entirely when zero, which is what proto3 does on the wire."""
    return b"" if not value else _tag(field, 0) + _varint(value)


def _int(field: int, value: int) -> bytes:
    """A signed ``int32`` field, which proto3 encodes as a 64-bit two's complement varint.

    Not zig-zag — that is ``sint32``, a different wire encoding — so a negative always costs ten
    bytes. Getting this wrong is not a size bug: the server would read a wildly large positive
    version and the listing would be published with a ceiling nobody could reach.
    """
    if not value:
        return b""
    return _tag(field, 0) + _varint(value & 0xFFFFFFFFFFFFFFFF)


def _delimited(field: int, payload: bytes) -> bytes:
    """A length-delimited field: string, bytes, or a nested message."""
    return _tag(field, 2) + _varint(len(payload)) + payload


def _text(field: int, value: str) -> bytes:
    """A string field. Sent even when empty — see :data:`DESCRIPTION_FILE` for why that matters."""
    return _delimited(field, value.encode("utf-8"))


def addon_branch(min_version: int, max_version: int) -> bytes:
    """Encode one ``AddonBranch``.

    ``max_version`` goes through :func:`_int`, not :func:`_uint`: it is a signed field and the
    value that means "and everything newer" is negative.
    """
    return _uint(1, min_version) + _int(2, max_version)


def addon_info(
    *, addon_id: int, title: str, tags: str, support_url: str, description: str
) -> bytes:
    """Encode the ``AddonInfo`` for this listing, with its single version branch."""
    return (
        _uint(1, addon_id)
        + _text(2, title)
        + _text(3, tags)
        + _text(4, support_url)
        + _text(5, description)
        + _delimited(6, addon_branch(MIN_POINT_VERSION, MAX_POINT_VERSION))
    )


def addon_file(zip_data: bytes, branch_index: int = 0) -> bytes:
    """Encode the ``AddonFile`` carrying the package."""
    return _delimited(1, zip_data) + _uint(2, branch_index)


def upload_request(info: bytes, file: bytes) -> bytes:
    """Encode the ``UploadAddonRequest``."""
    return _delimited(1, info) + _delimited(2, file)


def read_addon_id(payload: bytes) -> int:
    """Read ``UploadAddonResponse.addon_id`` out of a response body.

    Deliberately minimal: it walks to field 1 and reads its varint, ignoring anything else the
    server may add later. A response that carries no field 1 returns 0, which the caller treats
    as "it answered, but not with an id" rather than as success.
    """
    index = 0
    while index < len(payload):
        key, index = _read_varint(payload, index)
        field, wire = key >> 3, key & 0x07
        if wire == 0:
            value, index = _read_varint(payload, index)
            if field == 1:
                return value
        elif wire == 2:
            length, index = _read_varint(payload, index)
            index += length
        else:  # a wire type this response is not expected to use
            break
    return 0


def _read_varint(payload: bytes, index: int) -> tuple[int, int]:
    """Read one varint, returning ``(value, next_index)``."""
    value = shift = 0
    while index < len(payload):
        byte = payload[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise PublishError("the server's answer ended mid-number")


# --- the upload ----------------------------------------------------------------------------


def description() -> str:
    """The listing text to publish, or raise when it is missing.

    Raising rather than defaulting to "": an empty description would overwrite the page with
    nothing, and a release that quietly erases the listing's text is worse than one that stops.
    """
    if not DESCRIPTION_FILE.is_file():
        raise PublishError(
            f"{DESCRIPTION_FILE.name} is missing — it is the listing's description, and "
            "uploading without it would blank the page"
        )
    text = DESCRIPTION_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise PublishError(f"{DESCRIPTION_FILE.name} is empty — see above")
    return text


def publish(package: Path, version: str, cookie: str) -> int:
    """Upload ``package`` to the listing and return the add-on id AnkiWeb answered with.

    Args:
        package: The built ``.ankiaddon``.
        version: The tag being published, for the log line only — the version a user sees comes
            from ``human_version`` inside the package, stamped at build time.
        cookie: The session, straight from the environment.

    Returns:
        The add-on id from the response.

    Raises:
        PublishError: On anything that means the listing was not updated, with the status code
            and a sentence — never the response body, which can echo a request header back into
            a public log.
    """
    if not package.is_file():
        raise PublishError(f"no package to upload at {package}")
    body = upload_request(
        addon_info(
            addon_id=ADDON_ID,
            title=TITLE,
            tags=TAGS,
            support_url=SUPPORT_URL,
            description=description(),
        ),
        addon_file(package.read_bytes()),
    )
    request = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            # Both parts matter: the site sets `has_auth` alongside the session and the endpoint
            # expects the pair.
            "Cookie": f"has_auth=1; ankiweb={cookie}",
            "User-Agent": "omnia-release",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            answer = response.read()
    except urllib.error.HTTPError as exc:
        raise PublishError(_explain(exc.code)) from None
    except urllib.error.URLError as exc:
        raise PublishError(f"could not reach AnkiWeb: {exc.reason}") from None
    addon_id = read_addon_id(answer)
    if not addon_id:
        raise PublishError(
            "AnkiWeb accepted the request but did not answer with an add-on id, so the "
            "listing may not have been updated"
        )
    print(f"Uploaded {package.name} ({version}) to add-on {addon_id}.")
    return addon_id


def _explain(status: int) -> str:
    """What a status code means here, in words a log reader can act on."""
    if status in (401, 403):
        return (
            f"AnkiWeb refused the session ({status}). Log in at ankiweb.net, take the cookie "
            "again and update the ANKIWEB_COOKIE secret — logging out is also what revokes the "
            "old one."
        )
    if status == 400:
        return (
            "AnkiWeb rejected the upload (400, and it sends no reason). The usual cause is a "
            "version branch whose month is not a real month — check MIN/MAX_POINT_VERSION."
        )
    if status == 413:
        return "AnkiWeb refused the package as too large (413)."
    return f"AnkiWeb answered {status}."


def main(argv: list[str]) -> int:
    """Entry point: ``publish_ankiweb.py <package> <version>``."""
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    cookie = os.environ.get("OMNIA_ANKIWEB_COOKIE", "").strip()
    if not cookie:
        print(
            "OMNIA_ANKIWEB_COOKIE is not set — nothing was uploaded.",
            file=sys.stderr,
        )
        return 1
    try:
        publish(Path(argv[1]), argv[2], cookie)
    except PublishError as exc:
        print(f"AnkiWeb upload failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    enable_utf8_output()
    raise SystemExit(main(sys.argv))
