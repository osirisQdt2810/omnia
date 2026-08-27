"""Tests for `GET /media` on the word-lookup service.

The endpoint exists because the panel's image preview used to fetch through AnkiConnect -- a
SEPARATE add-on. On a machine without it (verified: no add-on was serving 8765, while omnia's
own 8766 answered fine) every preview reported "Image unavailable" although the lookup itself
worked, because the lookup is this service and the image was not.

It reads files off disk on a loopback socket, so the name checks are the part that matters.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from omnia.plugins.word_lookup.service import LookupService


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def served(tmp_path):
    """A running service whose media folder is `tmp_path`."""
    media = tmp_path / "collection.media"
    media.mkdir()
    (media / "picture.png").write_bytes(bytes.fromhex("89504e470d0a1a0a"))
    (media / "clip.mp3").write_bytes(b"ID3 fake")
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")

    service = LookupService(
        lambda word: {"word": word, "found": False},
        media_dir=lambda: str(media),
        port=_free_port(),
    )
    assert service.start(), "the service did not bind"
    try:
        yield service, media
    finally:
        service.stop()


def _get(service: LookupService, path: str):
    url = f"http://127.0.0.1:{service._port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return (
                response.status,
                response.headers.get("Content-Type"),
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type"), error.read()


class TestServingMedia:
    def test_it_returns_the_file_bytes(self, served) -> None:
        service, media = served

        status, content_type, body = _get(service, "/media?file=picture.png")

        assert status == 200
        assert body == (media / "picture.png").read_bytes()
        assert content_type == "image/png"

    def test_audio_is_served_too(self, served) -> None:
        """The panel's Play button needs the same fetcher the images do."""
        service, _media = served

        status, content_type, _body = _get(service, "/media?file=clip.mp3")

        assert status == 200
        assert content_type == "audio/mpeg"

    def test_an_unknown_extension_still_serves(self, served) -> None:
        service, media = served
        (media / "odd.xyz").write_bytes(b"bytes")

        status, content_type, body = _get(service, "/media?file=odd.xyz")

        assert status == 200
        assert body == b"bytes"
        assert content_type == "application/octet-stream"

    @pytest.mark.parametrize("name", ["diagram..png", "a..b..c.png", "..leading.png"])
    def test_a_dotted_name_is_not_a_traversal(self, served, name: str) -> None:
        """Refusing ".." as a SUBSTRING also refuses real files people actually have.

        Anki keeps whatever name the note referenced, and a double dot inside one is legal.
        The check asks whether the name IS a bare file name, not whether it happens to contain
        a suspicious sequence.
        """
        _service, media = served
        (media / name).write_bytes(b"real file")

        status, _content_type, body = _get(_service, f"/media?file={name}")

        assert status == 200, f"{name!r} is a legitimate media name and was refused"
        assert body == b"real file"

    def test_a_missing_file_is_404_not_a_crash(self, served) -> None:
        service, _media = served

        status, _content_type, body = _get(service, "/media?file=absent.png")

        assert status == 404
        assert json.loads(body)["error"]

    def test_no_filename_is_400(self, served) -> None:
        service, _media = served

        status, _content_type, _body = _get(service, "/media?file=")

        assert status == 400


class TestItWillNotReadTheDisk:
    """A loopback file reader is still a file reader; the name checks are the whole guard."""

    @pytest.mark.parametrize(
        "attempt",
        [
            "../secret.txt",
            "..%2Fsecret.txt",
            "subdir/picture.png",
            "..\\secret.txt",
            "C:\\Windows\\win.ini",
            "/etc/passwd",
        ],
    )
    def test_a_path_is_refused(self, served, attempt: str) -> None:
        service, _media = served

        status, _content_type, _body = _get(service, f"/media?file={attempt}")

        assert status == 404, f"{attempt!r} was served"

    def test_a_symlink_out_of_the_folder_is_refused(self, served, tmp_path) -> None:
        """The resolved path must sit inside the media folder, whatever the name looked like."""
        import os

        service, media = served
        try:
            os.symlink(tmp_path / "secret.txt", media / "escape.png")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")

        status, _content_type, _body = _get(service, "/media?file=escape.png")

        assert status == 404


class TestWithoutAMediaFolder:
    @pytest.mark.parametrize("injected", [None, lambda: "", lambda: None])
    def test_media_is_disabled_when_there_is_no_folder(
        self, injected, tmp_path, monkeypatch
    ) -> None:
        """`None` is the shape tests inject; `""` is the shape PRODUCTION returns.

        `WordLookupPlugin._media_dir` answers "" when there is no collection, and every failure
        inside it funnels into that same "". Guarding on the callable rather than on its result
        let `Path("").resolve()` become the process's working directory, so a bare name served
        whatever happened to sit next to Anki -- a loopback file reader over a directory nobody
        chose. Testing only the `None` shape passed while exactly that was true.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cwdsecret.png").write_bytes(b"CWD SECRET BYTES")

        kwargs = {} if injected is None else {"media_dir": injected}
        service = LookupService(
            lambda word: {"word": word}, port=_free_port(), **kwargs
        )
        assert service.start()
        try:
            status, _content_type, body = _get(service, "/media?file=cwdsecret.png")
        finally:
            service.stop()

        assert status == 404, f"the working directory was served: {body!r}"

    def test_the_folder_is_read_per_request_not_once(self, tmp_path) -> None:
        """Switching Anki profiles swaps the collection, so a cached folder would go stale.

        This is the entire reason the constructor takes a callable rather than a path, and
        nothing pinned it.
        """
        first = tmp_path / "first.media"
        second = tmp_path / "second.media"
        first.mkdir()
        second.mkdir()
        (first / "shared.png").write_bytes(b"FIRST")
        (second / "shared.png").write_bytes(b"SECOND")
        current = {"dir": first}

        service = LookupService(
            lambda word: {"word": word},
            media_dir=lambda: str(current["dir"]),
            port=_free_port(),
        )
        assert service.start()
        try:
            assert _get(service, "/media?file=shared.png")[2] == b"FIRST"
            current["dir"] = second
            assert _get(service, "/media?file=shared.png")[2] == b"SECOND"
        finally:
            service.stop()

    def test_lookup_still_works(self, served) -> None:
        """The new route must not have shadowed the old one."""
        service, _media = served

        status, content_type, body = _get(service, "/lookup?word=hello")

        assert status == 200
        assert "json" in (content_type or "")
        assert json.loads(body)["word"] == "hello"
