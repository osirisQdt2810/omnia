"""Tests for moving a package from one machine to the other.

A real session on loopback, a real client against it, and real bytes on disk. The transfer is the
part that cannot be reasoned about from the types — a Content-Length, a chunk boundary, a peer
that goes away half way — and a fake on both sides would agree with itself about all three.

What is pinned here beyond the happy path: a package is fetched ONCE and then gone, a truncated
transfer leaves nothing behind, and a source that cannot pack says so instead of failing oddly.
"""

from __future__ import annotations

import os

import pytest

from omnia.core.sync import (
    Inventory,
    PairingAddress,
    Session,
    SyncClient,
    SyncError,
    new_token,
)
from omnia.core.sync.package import PackageOffer, PackageRequest

PAYLOAD = (
    b"anki-package-bytes " * 40_000
)  # ~760 KB: several chunks, so boundaries are exercised


def _request() -> PackageRequest:
    return PackageRequest(decks=("Japanese",), note_types=("Basic",))


@pytest.fixture
def packed(tmp_path):
    """A session that packs a fixed payload, and a client holding its code."""
    made: list[str] = []

    def packager(request: PackageRequest):
        path = tmp_path / f"package-{len(made)}.apkg"
        path.write_bytes(PAYLOAD)
        made.append(str(path))
        return str(path), PackageOffer(
            id=f"pkg{len(made)}", bytes=len(PAYLOAD), cards=12, notes=9
        )

    key = new_token()
    session = Session(
        lambda: Inventory(), key=key, host="127.0.0.1", port=0, packager=packager
    )
    assert session.start()
    try:
        client = SyncClient(PairingAddress("127.0.0.1", session.port, key), timeout=10)
        yield session, client, made, tmp_path
    finally:
        session.stop()


class TestAPackageCrossesWhole:
    def test_the_size_is_known_before_a_byte_moves(self, packed):
        # The whole reason this is two steps: a percentage and a time estimate need a total, and
        # learning it only as the last chunk arrives is learning it too late.
        _session, client, _made, _tmp = packed

        offer = client.request_package(_request())

        assert offer.bytes == len(PAYLOAD)
        assert offer.cards == 12

    def test_the_bytes_arrive_intact(self, packed):
        _session, client, _made, tmp = packed
        offer = client.request_package(_request())
        destination = str(tmp / "arrived.apkg")

        received = client.download_package(offer, destination)

        assert received == len(PAYLOAD)
        assert open(destination, "rb").read() == PAYLOAD

    def test_progress_is_reported_as_it_goes(self, packed):
        # Not once at the end: the point is a bar that moves during a transfer that takes
        # minutes, and one callback at 100% is a bar that jumps.
        _session, client, _made, tmp = packed
        offer = client.request_package(_request())
        seen: list[int] = []

        client.download_package(offer, str(tmp / "a.apkg"), on_progress=seen.append)

        assert len(seen) > 1, "progress was reported once, so nothing would move"
        assert seen == sorted(seen), "progress went backwards"
        assert seen[-1] == len(PAYLOAD)


class TestWhatTheSourceDoesNotKeep:
    def test_a_package_is_fetched_once_and_then_gone(self, packed):
        # This machine is not keeping a copy of somebody's decks on the chance they ask again.
        _session, client, made, tmp = packed
        offer = client.request_package(_request())
        client.download_package(offer, str(tmp / "a.apkg"))

        assert not os.path.exists(made[0])
        with pytest.raises(SyncError, match="no longer waiting"):
            client.download_package(offer, str(tmp / "b.apkg"))

    def test_stopping_the_session_discards_what_was_never_collected(self, packed):
        # A peer that asks and then closes its laptop leaves a package behind. Hundreds of
        # megabytes of temp file per attempt is not an acceptable way to find that out.
        session, client, made, _tmp = packed
        client.request_package(_request())
        assert os.path.exists(made[0])

        session.stop()

        assert not os.path.exists(made[0])

    def test_an_id_that_was_never_offered_is_refused(self, packed):
        _session, client, _made, tmp = packed

        with pytest.raises(SyncError, match="no longer waiting"):
            client.download_package(PackageOffer(id="made-up"), str(tmp / "a.apkg"))


class TestWhenItGoesWrong:
    def test_a_truncated_transfer_leaves_nothing_behind(self, packed, monkeypatch):
        # An .apkg that is 90% of a package is not a smaller package — Anki refuses it — and
        # leaving it on disk invites a retry that finds it there and appears to work.
        _session, client, _made, tmp = packed
        offer = client.request_package(_request())
        destination = str(tmp / "half.apkg")

        def stop_half_way(total: int) -> None:
            if total > len(PAYLOAD) // 2:
                raise OSError("the network went away")

        with pytest.raises(SyncError, match="stopped part way"):
            client.download_package(offer, destination, on_progress=stop_half_way)

        assert not os.path.exists(destination)

    def test_a_short_answer_is_caught_by_the_size_that_was_promised(
        self, packed, tmp_path
    ):
        # The source said how many bytes; fewer arriving is a broken transfer even when the
        # connection closed politely.
        _session, client, _made, tmp = packed
        offer = client.request_package(_request())
        lying = PackageOffer(id=offer.id, bytes=offer.bytes * 2)
        destination = str(tmp / "short.apkg")

        with pytest.raises(SyncError, match="stopped part way"):
            client.download_package(lying, destination)

        assert not os.path.exists(destination)

    def test_a_source_that_cannot_pack_says_so_rather_than_failing_oddly(
        self, tmp_path
    ):
        # An older Omnia over there: it can list what it has and cannot hand any of it over.
        key = new_token()
        session = Session(lambda: Inventory(), key=key, host="127.0.0.1", port=0)
        assert session.start()
        try:
            client = SyncClient(
                PairingAddress("127.0.0.1", session.port, key), timeout=10
            )

            with pytest.raises(SyncError, match="update Omnia there"):
                client.request_package(_request())
        finally:
            session.stop()

    def test_a_request_that_means_nothing_is_refused_by_the_source_too(self, packed):
        # The check lives on PackageRequest, so it holds at BOTH ends — a peer that skipped it
        # cannot talk this machine into a wider export.
        _session, client, _made, _tmp = packed
        raw = b'{"decks": ["Japanese"], "note_types": []}'

        with pytest.raises(SyncError, match="left behind"):
            client._send("/sync/package", raw)

    def test_a_wrong_code_cannot_ask_for_a_package(self, packed):
        session, _client, _made, _tmp = packed
        stranger = SyncClient(
            PairingAddress("127.0.0.1", session.port, "000000000"), timeout=10
        )

        with pytest.raises(SyncError, match="does not open"):
            stranger.request_package(_request())


class TestTheCollectionIsNeverReachedForAnUnauthorisedRequest:
    def test_nothing_is_packed_for_a_peer_that_did_not_prove_the_code(self, tmp_path):
        # The refusal happens before the packager is called, so a stranger cannot make this
        # machine spend minutes exporting media.
        packed_for: list[str] = []

        def packager(request):
            packed_for.append("someone")
            return "", PackageOffer(id="x")

        session = Session(
            lambda: Inventory(),
            key=new_token(),
            host="127.0.0.1",
            port=0,
            packager=packager,
        )
        assert session.start()
        try:
            stranger = SyncClient(
                PairingAddress("127.0.0.1", session.port, "111111111"), timeout=10
            )
            with pytest.raises(SyncError):
                stranger.request_package(_request())
        finally:
            session.stop()

        assert packed_for == []
