"""Tests for the on-demand piper voice store.

The voice weights no longer ship inside the ``.ankiaddon`` (they were ~99% of a 59 MB package),
so the provider now RESOLVES a voice: a previous download in ``user_files``, else the copy
beside the add-on, else a one-time fetch. That ladder decides whether a user waits 60 MB, and
its download path decides whether a truncated transfer can be played as a model — so every rung
is pinned here, with the network replaced by a scripted streamer. Nothing in this file touches
a real URL.
"""

from __future__ import annotations

import hashlib
import http.client
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.voice_models import (
    DOWNLOADABLE_VOICES,
    DownloadFeedback,
    PiperVoiceStore,
    _catalog_entry,
    bundled_models_dir,
)

_VOICE = "te_ST-fake-medium"

_WEIGHTS = b"ONNX-WEIGHT-BYTES" * 64
_CONFIG = b'{"audio": {"sample_rate": 22050}}'


def _catalog() -> dict:
    """A one-voice catalog whose expected sizes/digests describe the fake payloads above."""
    model = _catalog_entry(
        _VOICE,
        weights_sha256=hashlib.sha256(_WEIGHTS).hexdigest(),
        weights_size=len(_WEIGHTS),
        config_sha256=hashlib.sha256(_CONFIG).hexdigest(),
        config_size=len(_CONFIG),
    )
    return {model.voice: model}


class _FakeStreamer:
    """Serves scripted bytes per URL suffix; records every URL it was asked for."""

    def __init__(self, weights: bytes = _WEIGHTS, config: bytes = _CONFIG) -> None:
        self._bodies = {".onnx": weights, ".onnx.json": config}
        self.urls: list[str] = []

    def stream(self, url):
        self.urls.append(url)
        body = self._bodies[".onnx.json" if ".onnx.json" in url else ".onnx"]
        # Chunked on purpose: the store hashes and counts incrementally, so a single-chunk fake
        # would not exercise the loop that a real 60 MB transfer runs.
        for start in range(0, len(body), 16):
            yield body[start : start + 16]


class _ExplodingStreamer:
    """A transport that fails the way a dead network does — used to prove nothing fetched."""

    def __init__(self, message: str = "getaddrinfo failed") -> None:
        self._message = message
        self.calls = 0

    def stream(self, url):
        self.calls += 1
        raise ProviderError(f"network error reaching {url}: {self._message}")
        yield b""  # pragma: no cover - makes this a generator function


class _StreamerTouched(BaseException):
    """Deliberately NOT an ``Exception``.

    The store funnels every ``Exception`` into a ``ProviderError`` naming the voice — so an
    ``AssertionError`` raised by a "must not be called" fake would be caught and re-raised as
    exactly the error type the test was asserting, and the test would pass green while the
    download it existed to prevent ran in full. Deriving from ``BaseException`` puts it outside
    that funnel, which is what makes "nothing was fetched" genuinely pinned.
    """


class _ForbiddenStreamer:
    """A transport whose every use fails the test outright — see :class:`_StreamerTouched`."""

    def stream(self, url):
        raise _StreamerTouched(
            f"the voice store must not have fetched anything ({url})"
        )
        yield b""  # pragma: no cover - makes this a generator function


class _RawFailureStreamer:
    """Fails with an exception that is NEITHER ``ProviderError`` NOR ``OSError``.

    ``http.client.IncompleteRead`` is what a chunked 60 MB transfer raises when the connection
    drops mid-body — the realistic failure that walked past both handlers and reached the user
    as a raw traceback.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def stream(self, url):
        self.calls += 1
        yield b"partial-bytes"
        raise self._error


class _Recorder(DownloadFeedback):
    """Collects the status lines a download reports; optionally cancels after N of them."""

    def __init__(self, cancel_after: int | None = None) -> None:
        self.lines: list[str] = []
        self._cancel_after = cancel_after

    def report(self, message: str) -> None:
        self.lines.append(message)

    def cancelled(self) -> bool:
        return self._cancel_after is not None and len(self.lines) >= self._cancel_after


def _store(tmp_path: Path, streamer=None) -> PiperVoiceStore:
    return PiperVoiceStore(
        user_dir=tmp_path / "user_files" / "models" / "piper",
        bundled_dir=tmp_path / "bundled" / "models" / "piper",
        streamer=streamer if streamer is not None else _ExplodingStreamer(),
        catalog=_catalog(),
    )


def _write(directory: Path, name: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


class TestVoiceResolutionOrder:
    """user_files wins over the bundled copy, and both win over the network."""

    def test_the_user_files_copy_wins_over_the_bundled_one(self, tmp_path):
        streamer = _ExplodingStreamer()
        store = _store(tmp_path, streamer)
        user_dir = tmp_path / "user_files" / "models" / "piper"
        bundled = tmp_path / "bundled" / "models" / "piper"
        _write(user_dir, f"{_VOICE}.onnx", _WEIGHTS)
        _write(user_dir, f"{_VOICE}.onnx.json", _CONFIG)
        _write(bundled, f"{_VOICE}.onnx", _WEIGHTS)
        _write(bundled, f"{_VOICE}.onnx.json", _CONFIG)

        resolved = store.resolve(_VOICE)

        assert resolved == user_dir / f"{_VOICE}.onnx"
        assert streamer.calls == 0

    def test_the_bundled_copy_wins_over_downloading(self, tmp_path):
        """A developer checkout (Git LFS) or a hand-installed voice must never re-download."""
        streamer = _ExplodingStreamer()
        store = _store(tmp_path, streamer)
        bundled = tmp_path / "bundled" / "models" / "piper"
        _write(bundled, f"{_VOICE}.onnx", _WEIGHTS)
        _write(bundled, f"{_VOICE}.onnx.json", _CONFIG)

        assert store.resolve(_VOICE) == bundled / f"{_VOICE}.onnx"
        assert streamer.calls == 0

    def test_an_lfs_pointer_is_not_mistaken_for_a_voice(self, tmp_path):
        """CI checks out without ``git lfs pull``: the ".onnx" there is a ~130-byte text file.

        It is a file, so an existence check would hand it to piper as a model. The size check is
        what makes the store fall through to a real download instead.
        """
        streamer = _FakeStreamer()
        store = _store(tmp_path, streamer)
        bundled = tmp_path / "bundled" / "models" / "piper"
        _write(
            bundled, f"{_VOICE}.onnx", b"version https://git-lfs.github.com/spec/v1\n"
        )
        _write(bundled, f"{_VOICE}.onnx.json", _CONFIG)

        resolved = store.resolve(_VOICE)

        assert (
            resolved == tmp_path / "user_files" / "models" / "piper" / f"{_VOICE}.onnx"
        )
        assert resolved.read_bytes() == _WEIGHTS

    def test_an_off_catalog_voice_is_accepted_from_disk_as_is(self, tmp_path):
        """A voice the user supplied has no expected size, so existence is all we can ask."""
        store = _store(tmp_path)
        bundled = tmp_path / "bundled" / "models" / "piper"
        _write(bundled, "en_US-someone-low.onnx", b"whatever")

        assert store.resolve("en_US-someone-low") == bundled / "en_US-someone-low.onnx"

    def test_local_path_never_downloads_and_falls_back_to_the_bundled_dir(
        self, tmp_path
    ):
        streamer = _ExplodingStreamer()
        store = _store(tmp_path, streamer)
        bundled = tmp_path / "bundled" / "models" / "piper"

        assert store.local_path("nothing.onnx") == bundled / "nothing.onnx"
        assert streamer.calls == 0

        user_copy = _write(
            tmp_path / "user_files" / "models" / "piper", "nothing.onnx", b"x"
        )
        assert store.local_path("nothing.onnx") == user_copy


class TestVoiceDownload:
    def test_a_missing_voice_is_fetched_into_user_files_with_its_config(self, tmp_path):
        streamer = _FakeStreamer()
        store = _store(tmp_path, streamer)
        progress = _Recorder()

        resolved = store.resolve(_VOICE, feedback=progress)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert resolved == user_dir / f"{_VOICE}.onnx"
        assert resolved.read_bytes() == _WEIGHTS
        # The .onnx.json MUST land beside the weights — piper finds it by guessing that path.
        assert (user_dir / f"{_VOICE}.onnx.json").read_bytes() == _CONFIG
        assert all(url.startswith("https://huggingface.co/") for url in streamer.urls)
        assert any("%" in line for line in progress.lines)  # a percentage was reported

    def test_a_second_resolve_reuses_the_download(self, tmp_path):
        store = _store(tmp_path, _FakeStreamer())
        store.resolve(_VOICE)

        store._streamer = _ExplodingStreamer()  # any further fetch would raise
        assert store.resolve(_VOICE).read_bytes() == _WEIGHTS

    def test_no_temp_files_survive_a_successful_download(self, tmp_path):
        store = _store(tmp_path, _FakeStreamer())
        store.resolve(_VOICE)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not list(user_dir.glob("*.part"))


class TestPartialAndCorruptDownloads:
    """A partial file must never be mistaken for a complete one."""

    def test_a_short_download_is_rejected_and_nothing_is_left_behind(self, tmp_path):
        store = _store(tmp_path, _FakeStreamer(weights=_WEIGHTS[:100]))

        with pytest.raises(ProviderError, match="stopped early"):
            store.resolve(_VOICE)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not (user_dir / f"{_VOICE}.onnx").exists()
        assert not list(user_dir.glob("*.part"))

    def test_a_corrupted_download_of_the_right_length_is_rejected(self, tmp_path):
        corrupt = b"X" * len(_WEIGHTS)
        store = _store(tmp_path, _FakeStreamer(weights=corrupt))

        with pytest.raises(ProviderError, match="corrupted"):
            store.resolve(_VOICE)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not (user_dir / f"{_VOICE}.onnx").exists()

    def test_a_rejected_download_is_retried_rather_than_reused(self, tmp_path):
        store = _store(tmp_path, _FakeStreamer(weights=_WEIGHTS[:100]))
        with pytest.raises(ProviderError):
            store.resolve(_VOICE)

        store._streamer = _FakeStreamer()
        assert store.resolve(_VOICE).read_bytes() == _WEIGHTS


class TestUnanticipatedTransportFailures:
    """The failures that hurt are the classes nobody listed. Every one still lands as ONE
    ``ProviderError`` naming the voice, with no ``.part`` left in ``user_files``.
    """

    def test_an_incomplete_chunked_read_is_reported_not_raised_raw(self, tmp_path):
        """The regression: a dropped chunked transfer.

        ``http.client.IncompleteRead`` is neither ``OSError`` nor ``ProviderError``, so it used
        to walk past the streamer's handler AND the fetch's, reaching the user as
        ``IncompleteRead(0 bytes read, 5000 more expected)`` — naming no voice and no way out —
        while leaving an up-to-60 MB orphan in the one directory Anki never clears.
        """
        streamer = _RawFailureStreamer(http.client.IncompleteRead(b"partial", 5000))
        store = _store(tmp_path, streamer)

        with pytest.raises(ProviderError) as excinfo:
            store.resolve(_VOICE)

        message = str(excinfo.value)
        assert _VOICE in message
        assert (
            "IncompleteRead" in message
        )  # the cause survives into the legible message
        assert "internet connection" in message
        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not list(user_dir.glob("*.part"))
        assert not (user_dir / f"{_VOICE}.onnx").exists()

    def test_the_urllib_streamer_converts_an_http_exception(self):
        """``IncompleteRead`` raised INSIDE the streamer is converted there too, not only at
        the fetch boundary — the ``ByteStreamer`` protocol promises ProviderError.
        """
        from omnia.core.providers.tts.voice_models import UrllibByteStreamer

        def _urlopen(url, timeout=None):
            raise http.client.IncompleteRead(b"", 5000)

        streamer = UrllibByteStreamer()
        with mock.patch("urllib.request.urlopen", _urlopen):
            with pytest.raises(ProviderError, match="network error"):
                list(streamer.stream("https://example.invalid/voice.onnx"))

    def test_an_exception_class_nobody_listed_still_names_the_voice(self, tmp_path):
        store = _store(tmp_path, _RawFailureStreamer(RuntimeError("proxy exploded")))

        with pytest.raises(ProviderError, match=_VOICE):
            store.resolve(_VOICE)

        assert not list((tmp_path / "user_files" / "models" / "piper").glob("*.part"))

    def test_an_interrupt_is_not_swallowed_but_still_takes_the_temp_file(
        self, tmp_path
    ):
        """A KeyboardInterrupt must stay a KeyboardInterrupt — and still leave no orphan.

        That is why the cleanup is a ``finally`` rather than one more ``except`` arm.
        """
        store = _store(tmp_path, _RawFailureStreamer(KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            store.resolve(_VOICE)

        assert not list((tmp_path / "user_files" / "models" / "piper").glob("*.part"))


class TestOversizedDownload:
    """A body longer than the pinned size is refused while it streams, not after it lands."""

    def test_an_over_long_body_is_rejected_without_writing_past_the_pinned_size(
        self, tmp_path
    ):
        # Ten times the expected bytes: a broken proxy, a changed upstream object, or a
        # hostile server. The store must not write it all out and only then complain.
        store = _store(tmp_path, _FakeStreamer(weights=_WEIGHTS * 10))

        with pytest.raises(ProviderError, match="larger than the expected"):
            store.resolve(_VOICE)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not (user_dir / f"{_VOICE}.onnx").exists()
        assert not list(user_dir.glob("*.part"))

    def test_the_transfer_stops_at_the_first_over_long_chunk(self, tmp_path):
        """Pins the BOUND, not merely the rejection.

        "We rejected it" is no comfort if the disk filled up on the way to finding out, so this
        asserts the store stopped CONSUMING the body — measured on the streamer, which no
        write-buffering can hide — instead of draining all ten copies first.
        """

        class _CountingStreamer(_FakeStreamer):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.served = 0

            def stream(self, url):
                for chunk in super().stream(url):
                    self.served += len(chunk)
                    yield chunk

        streamer = _CountingStreamer(weights=_WEIGHTS * 10)
        store = _store(tmp_path, streamer)

        with pytest.raises(ProviderError, match="larger than the expected"):
            store.resolve(_VOICE)

        # At most the pinned size plus the one chunk whose arrival triggered the refusal.
        assert streamer.served <= len(_WEIGHTS) + 16
        assert not list((tmp_path / "user_files" / "models" / "piper").glob("*.part"))


class TestCancellation:
    """The Cancel button on the progress dialog actually stops a multi-minute fetch."""

    def test_cancelling_stops_the_download_and_keeps_nothing(self, tmp_path):
        streamer = _FakeStreamer()
        store = _store(tmp_path, streamer)
        # Cancel once the first progress line has been reported — i.e. mid-transfer.
        feedback = _Recorder(cancel_after=1)

        with pytest.raises(ProviderError, match="cancelled"):
            store.resolve(_VOICE, feedback=feedback)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not (user_dir / f"{_VOICE}.onnx").exists()
        assert not list(user_dir.glob("*.part"))

    def test_a_cancelled_download_does_not_blame_the_network(self, tmp_path):
        """The user chose to stop; "check your internet connection" would be a lie."""
        store = _store(tmp_path, _FakeStreamer())

        with pytest.raises(ProviderError) as excinfo:
            store.resolve(_VOICE, feedback=_Recorder(cancel_after=1))

        message = str(excinfo.value)
        assert _VOICE in message
        assert "internet connection" not in message

    def test_a_cancelled_download_is_retried_cleanly_afterwards(self, tmp_path):
        store = _store(tmp_path, _FakeStreamer())
        with pytest.raises(ProviderError):
            store.resolve(_VOICE, feedback=_Recorder(cancel_after=1))

        assert store.resolve(_VOICE).read_bytes() == _WEIGHTS


class TestStaleTempFiles:
    """``user_files`` is preserved across every add-on update, so an orphan there is forever."""

    def test_a_previous_runs_part_file_is_swept_before_downloading(self, tmp_path):
        user_dir = tmp_path / "user_files" / "models" / "piper"
        # What a kill -9 at 80% leaves behind: mkstemp's random name, never reused.
        orphan = _write(user_dir, f".{_VOICE}.onnx.abc123.part", b"X" * 4096)
        other = _write(user_dir, "unrelated.txt", b"keep me")
        store = _store(tmp_path, _FakeStreamer())

        store.resolve(_VOICE)

        assert not orphan.exists()
        assert other.exists()  # the sweep is scoped to this voice's own temp files
        assert not list(user_dir.glob("*.part"))

    def test_an_unremovable_part_file_does_not_fail_the_download(self, tmp_path):
        """Windows refuses to unlink a file another process holds open; that must not turn
        into "Could not download the piper voice" for a download that would have worked.
        """
        user_dir = tmp_path / "user_files" / "models" / "piper"
        _write(user_dir, f".{_VOICE}.onnx.abc123.part", b"X" * 16)
        store = _store(tmp_path, _FakeStreamer())

        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            resolved = store.resolve(_VOICE)

        assert resolved.read_bytes() == _WEIGHTS


class TestLegibleFailures:
    """No traceback reaches the reviewer: every failure names the voice and the way out."""

    def test_a_network_failure_names_the_voice_and_what_to_do(self, tmp_path):
        store = _store(tmp_path, _ExplodingStreamer("Name or service not known"))

        with pytest.raises(ProviderError) as excinfo:
            store.resolve(_VOICE)

        message = str(excinfo.value)
        assert _VOICE in message
        assert "Name or service not known" in message
        assert "internet connection" in message
        assert "huggingface.co" in message  # the manual fallback is spelled out

    def test_a_404_names_the_voice(self, tmp_path):
        store = _store(tmp_path, _ExplodingStreamer("HTTP 404"))

        with pytest.raises(ProviderError, match=_VOICE):
            store.resolve(_VOICE)

    def test_an_unknown_voice_says_which_voices_exist(self, tmp_path):
        store = _store(tmp_path)

        with pytest.raises(ProviderError) as excinfo:
            store.resolve("xx_XX-nobody-high")

        message = str(excinfo.value)
        assert "xx_XX-nobody-high" in message
        assert (
            _VOICE in message
        )  # the catalog is listed, so the user can pick a real one


class TestPiperTTSModelPath:
    """How ``PiperTTS`` turns a field's "voice" into a path — the store's only caller."""

    def _provider(self, tmp_path, streamer=None):
        from omnia.core.providers.tts.piper import PiperRunner, PiperTTS

        class _Runner(PiperRunner):
            """Always-ready runner: inherits the no-op ``ensure_ready`` from the seam."""

            def __init__(self):
                self.model_path = ""

            def run(self, text, model_path):
                self.model_path = model_path
                return b"RIFFwav"

        runner = _Runner()
        return (
            PiperTTS(runner=runner, store=_store(tmp_path, streamer)),
            runner,
        )

    def test_a_voice_name_is_resolved_through_the_store(self, tmp_path):
        provider, runner = self._provider(tmp_path, _FakeStreamer())

        assert provider.synthesize("xin chào", voice=_VOICE) == b"RIFFwav"
        assert runner.model_path == str(
            tmp_path / "user_files" / "models" / "piper" / f"{_VOICE}.onnx"
        )

    def test_an_absolute_onnx_path_is_used_verbatim(self, tmp_path):
        """The user's own model: no catalog, no download, no second-guessing the path."""
        provider, runner = self._provider(tmp_path)
        own = tmp_path / "elsewhere" / "my-voice.onnx"

        provider.synthesize("hi", voice=str(own))

        assert runner.model_path == str(own)

    def test_a_relative_onnx_name_is_looked_up_without_downloading(self, tmp_path):
        provider, runner = self._provider(tmp_path)
        dropped = _write(
            tmp_path / "bundled" / "models" / "piper", "dropped.onnx", b"onnx"
        )

        provider.synthesize("hi", voice="dropped.onnx")

        assert runner.model_path == str(dropped)


class TestAnkiProgressFeedback:
    """The bridge to Anki's dialog, exercised where there is no Anki (conftest sets mw None)."""

    def test_no_dialog_means_never_cancelled_rather_than_an_error(self):
        """A missing dialog must read as "nobody cancelled", not as a cancel and not as a crash.

        Getting this backwards would abort every download on a path that has no dialog — the
        review-time one — with a message blaming the user for a click they never made.
        """
        from omnia.core.providers.tts.piper import AnkiProgressFeedback

        assert AnkiProgressFeedback().cancelled() is False

    def test_reporting_without_a_dialog_is_silent(self):
        from omnia.core.providers.tts.piper import AnkiProgressFeedback

        AnkiProgressFeedback().report("voice.onnx: 4.2/63.2 MB (6%)")  # must not raise


class TestRuntimeIsCheckedBeforeAnythingIsFetched:
    """Ordering, pinned: the cheap always-failing check runs BEFORE the 60 MB one.

    Piper's native runtime is opt-in and OFF by default. Resolving the voice first meant a user
    who never enabled it waited out an entire download to be told the synthesis could not run —
    the exact inversion of ADR-005's "keep the slow, network-heavy install out of the synthesis
    path", now recorded as ADR-015.
    """

    def _provider_with_no_runtime(self, tmp_path):
        from omnia.core.providers.tts.piper import PiperTTS, SidecarPiperRunner
        from omnia.core.runtime.native_runtime import NativeRuntimeManager

        # A real manager over an empty envs dir: nothing installed, no marker, no auto-install.
        runner = SidecarPiperRunner(manager=NativeRuntimeManager(tmp_path / "envs"))
        # A streamer whose every use is an outright test failure, not a catchable error.
        return PiperTTS(runner=runner, store=_store(tmp_path, _ForbiddenStreamer()))

    def test_synthesis_without_the_runtime_never_touches_the_streamer(self, tmp_path):
        provider = self._provider_with_no_runtime(tmp_path)

        with pytest.raises(ProviderError, match="isn't installed"):
            provider.synthesize("xin chào", voice=_VOICE)

    def test_nothing_lands_on_disk_when_the_runtime_is_missing(self, tmp_path):
        provider = self._provider_with_no_runtime(tmp_path)

        with pytest.raises(ProviderError):
            provider.synthesize("xin chào", voice=_VOICE)

        user_dir = tmp_path / "user_files" / "models" / "piper"
        assert not user_dir.exists() or not list(user_dir.iterdir())


class TestShippedCatalog:
    """The real catalog entry must describe the voice the repo actually carries."""

    def test_the_default_voice_urls_are_derived_from_its_id(self):
        model = DOWNLOADABLE_VOICES["vi_VN-vais1000-medium"]

        assert model.weights.url.startswith(
            "https://huggingface.co/rhasspy/piper-voices/resolve/main"
            "/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx"
        )
        assert model.config.filename == "vi_VN-vais1000-medium.onnx.json"
        assert model.size_label == "63.2 MB"

    def test_the_pin_matches_the_git_lfs_pointer_in_every_checkout(self):
        """The digest pin, verified WITHOUT Git LFS — so CI actually checks it.

        The full-file test below can only run where ``git lfs pull`` has been done, and no
        workflow does: it therefore always skips, and a one-character typo in
        ``weights_sha256`` would merge green and break every user's first download with
        "checksum mismatch" and no way to install the voice by hand. The committed blob is the
        LFS *pointer* — it carries the same oid and size and is present in every clone — so
        reading it from git verifies the pin unconditionally.
        """
        model = DOWNLOADABLE_VOICES["vi_VN-vais1000-medium"]
        try:
            pointer = subprocess.run(
                [
                    "git",
                    "cat-file",
                    "-p",
                    f"HEAD:models/piper/{model.weights.filename}",
                ],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:  # no git binary (a source tarball, a minimal container)
            pytest.skip("git is not available here")
        if pointer.returncode != 0 or "git-lfs" not in pointer.stdout:
            pytest.skip("not a git checkout, or the voice is not an LFS pointer")

        assert f"oid sha256:{model.weights.sha256}" in pointer.stdout
        assert f"size {model.weights.size}" in pointer.stdout

    def test_the_pinned_digest_matches_the_checked_in_model(self):
        """The pin is the repo's own Git-LFS oid — verifiable here, not taken on trust.

        Skipped where the weights are absent (CI checks out without LFS), which is exactly the
        case the size check in the store is there to handle.
        """
        model = DOWNLOADABLE_VOICES["vi_VN-vais1000-medium"]
        local = bundled_models_dir() / model.weights.filename
        if not local.is_file() or local.stat().st_size != model.weights.size:
            pytest.skip("the LFS weights are not present in this checkout")

        digest = hashlib.sha256()
        with local.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        assert digest.hexdigest() == model.weights.sha256


class TestNamesThatUsedToWorkStillWork:
    """The catalog's own voice, spelled the way the shipped example config suggests.

    ``providers.example.toml`` documents ``model`` as "voice name ... or a path to a .onnx", and
    every setup made while the weights were packaged has some spelling of it on disk. Reading a
    bare ``<voice>.onnx`` as a FILE after the weights stopped shipping would break exactly those
    configs — silently, since the file it names genuinely used to exist.
    """

    def test_a_bare_catalog_voice_with_the_onnx_suffix_is_still_a_voice(self, tmp_path):
        store = PiperVoiceStore(user_dir=tmp_path / "u", bundled_dir=tmp_path / "b")

        assert store.knows("vi_VN-vais1000-medium")
        assert not store.knows(
            "vi_VN-vais1000-medium.onnx"
        )  # the id, not the file name

    def test_a_name_with_a_directory_is_still_a_path(self, tmp_path):
        # "voices/x.onnx" can only ever have been meant as a path, so it must not be
        # reinterpreted as a catalog id even if the stem happens to match one.
        store = PiperVoiceStore(user_dir=tmp_path / "u", bundled_dir=tmp_path / "b")

        assert not store.knows("voices/vi_VN-vais1000-medium")


class TestAnUnwritableDirectoryIsStillLegible:
    """A read-only add-on dir is the likeliest first-download failure, and it was escaping raw."""

    def test_mkstemp_permission_error_names_the_voice(self, tmp_path, monkeypatch):
        user = tmp_path / "u"
        user.mkdir(parents=True)
        store = PiperVoiceStore(
            user_dir=user, bundled_dir=tmp_path / "b", streamer=_ForbiddenStreamer()
        )

        def _denied(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(
            "omnia.core.providers.tts.voice_models.tempfile.mkstemp", _denied
        )

        with pytest.raises(ProviderError) as excinfo:
            store.resolve("vi_VN-vais1000-medium")

        message = str(excinfo.value)
        assert "vi_VN-vais1000-medium" in message  # the voice, not a random .part name
        assert ".part" not in message
