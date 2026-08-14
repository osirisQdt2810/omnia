"""Tests for the ``audio`` native runtime: the wrapper (``sidecar.py``) and the transcoder it
runs (``sidecar_cli.py``).

The wrapper's tests fake the :class:`~omnia.core.providers.native_runtime.NativeRuntimeManager`
and assert the CONTRACT around it — the spec the Advanced tab renders, the file-based argv
(never stdin/stdout, which Windows would corrupt), and the actionable error a caller gets when
the runtime is missing.

:class:`TestRealTranscode` then runs the transcoder for real. PyAV is a compiled wheel that only
ever exists inside the managed venv, so it is marked ``integration`` and SKIPS wherever ``av``
is absent (CI, and any dev box that has not installed it) — but it is the only coverage the
codec boundary can have, and without it a codec bug is invisible until a user hears it. Install
it with ``pip install av`` and run ``pytest -m integration tests/core/test_audio_sidecar.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnia.core.audio import sidecar_cli
from omnia.core.audio.sidecar import SPEC, AudioSidecar
from omnia.core.audio.wav import SAMPLE_WIDTH, WavClip
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.native_runtime import (
    NATIVE_RUNTIMES,
    native_runtimes_by_section,
)


class _FakeManager:
    """A manager whose ``run_in_venv`` writes a canned payload to the argv's output path."""

    def __init__(self, *, installed: bool = True, code: int = 0, output=b"OUT") -> None:
        self.installed = installed
        self.code = code
        self.output = output
        self.calls: list[list[str]] = []

    def is_installed(self, spec) -> bool:
        return self.installed

    def run_in_venv(self, spec, extra_argv, *, input=None):
        self.calls.append(list(extra_argv))
        if not self.installed:
            raise ProviderError(
                f"{spec.label} isn't installed — enable it in Smart Notes → Options → "
                "Advanced (native runtimes)."
            )
        self.seen_input = Path(extra_argv[1]).read_bytes()
        if self.output is not None:
            Path(extra_argv[2]).write_bytes(self.output)
        return self.code


class TestSpec:
    """What the Advanced tab and ADR-005 require of the declared runtime."""

    def test_is_registered_in_its_own_audio_section(self):
        assert NATIVE_RUNTIMES["audio"] is SPEC
        assert [spec.name for spec in native_runtimes_by_section()["audio"]] == [
            "audio"
        ]

    def test_installs_pyav_as_a_one_shot_cli(self):
        assert SPEC.pip_packages == ("av",)
        assert SPEC.mode == "cli"
        assert SPEC.label and SPEC.size_hint  # the toggle needs both to render

    def test_runs_the_bundled_script_with_the_venv_python(self):
        python, script = SPEC.cli_argv
        assert python == "{python}"
        assert Path(script).name == "sidecar_cli.py"
        assert Path(script).is_file()  # it ships with the package, not from pip

    def test_the_bundled_script_never_imports_av_at_module_level(self):
        # Anki's interpreter imports the package; a top-level `import av` there would explode
        # on every start. The import must live inside the transcode functions.
        source = Path(SPEC.cli_argv[1]).read_text(encoding="utf-8")
        assert "\nimport av" not in source
        assert "    import av" in source


class TestTranscode:
    """Both directions go through temp FILES, and report failure honestly."""

    def test_decode_passes_the_payload_by_file_and_returns_the_output(self):
        manager = _FakeManager(output=b"RIFFwav")
        assert AudioSidecar(manager).decode(b"ID3mp3") == b"RIFFwav"
        assert manager.calls[0][0] == "decode"
        assert manager.seen_input == b"ID3mp3"

    def test_encode_passes_the_payload_by_file_and_returns_the_output(self):
        manager = _FakeManager(output=b"ID3mp3")
        assert AudioSidecar(manager).encode(b"RIFFwav") == b"ID3mp3"
        assert manager.calls[0][0] == "encode"
        assert manager.seen_input == b"RIFFwav"

    def test_payloads_never_travel_on_stdin_or_stdout(self):
        # Windows opens the standard streams in text mode, so a 0x0D byte in an MP3 would be
        # rewritten in transit. Both sides must be paths.
        manager = _FakeManager()
        AudioSidecar(manager).decode(b"\r\n\r\n")
        command, source, target = manager.calls[0]
        assert command == "decode"
        assert Path(source) != Path(target)
        assert manager.seen_input == b"\r\n\r\n"

    def test_a_non_zero_exit_raises_an_actionable_error(self):
        sidecar = AudioSidecar(_FakeManager(code=3))
        with pytest.raises(ProviderError, match="could not decode"):
            sidecar.decode(b"x")

    def test_a_missing_output_file_raises(self):
        sidecar = AudioSidecar(_FakeManager(output=None))
        with pytest.raises(ProviderError, match="could not encode"):
            sidecar.encode(b"x")

    def test_temp_files_do_not_outlive_the_call(self):
        manager = _FakeManager()
        AudioSidecar(manager).decode(b"x")
        assert not Path(manager.calls[0][1]).exists()


class TestCommandLine:
    """The argv boundary — the parent process only ever sees an exit code."""

    def test_a_bad_command_is_refused_without_touching_a_codec(self):
        assert sidecar_cli.main(["transmogrify", "in", "out"]) == 2

    def test_a_missing_argument_is_refused(self):
        assert sidecar_cli.main(["decode", "in"]) == 2

    def test_anything_that_goes_wrong_exits_one_instead_of_raising(self, tmp_path):
        # A missing input (or, on a machine without the runtime, the import of `av` itself):
        # the parent reads the exit code, so the boundary must swallow and report, never crash.
        assert sidecar_cli.main(["decode", str(tmp_path / "nope"), "out"]) == 1


@pytest.mark.integration
class TestRealTranscode:
    """The codec boundary, run for real against PyAV (skipped when it isn't installed)."""

    @pytest.fixture(autouse=True)
    def av(self):
        """Skip the whole class without PyAV, and hand it to the tests that inspect a stream."""
        return pytest.importorskip(
            "av", reason="PyAV lives in the managed venv; `pip install av` to run these"
        )

    def _wav(self, path: Path, *, channels: int = 1, rate: int = 22050) -> WavClip:
        """Write a one-second tone at ``path`` and return the clip that was written."""
        base = WavClip(channels, SAMPLE_WIDTH, rate, b"\x00\x00" * channels)
        clip = WavClip.sine_beep(1000.0, 440.0, like=base)
        path.write_bytes(clip.to_bytes())
        return clip

    def _layout(self, av, path: Path) -> str:
        with av.open(str(path)) as container:
            return str(container.streams.audio[0].layout.name)

    def test_a_mono_voice_is_not_upmixed_to_stereo(self, av, tmp_path):
        # libmp3lame defaults to stereo, so an encoder configured only with a rate duplicates a
        # mono voice into two channels — doubling every clip the re-encode exists to keep small.
        source = tmp_path / "mono.wav"
        clip = self._wav(source, channels=1)
        target = tmp_path / "mono.mp3"

        sidecar_cli._encode(source, target)

        assert self._layout(av, target) == "mono"
        assert target.stat().st_size < len(clip.to_bytes()) / 2

    def test_a_stereo_voice_stays_stereo(self, av, tmp_path):
        source = tmp_path / "stereo.wav"
        self._wav(source, channels=2)
        target = tmp_path / "stereo.mp3"

        sidecar_cli._encode(source, target)

        assert self._layout(av, target) == "stereo"

    def test_the_round_trip_comes_back_as_a_spliceable_clip(self, tmp_path):
        # What cloze_audio actually does with an MP3 voice: decode to PCM, splice, re-encode.
        source = tmp_path / "voice.wav"
        clip = self._wav(source, channels=1)
        encoded = tmp_path / "voice.mp3"
        decoded = tmp_path / "voice-back.wav"

        sidecar_cli._encode(source, encoded)
        sidecar_cli._decode(encoded, decoded)

        back = WavClip.from_bytes(decoded.read_bytes())
        assert (
            back.params == clip.params
        )  # same channels, width and rate: it can be spliced
        # MP3 pads the stream with encoder delay, so the length is close, not equal.
        assert abs(back.duration_ms - clip.duration_ms) < 100

    def test_a_rate_libmp3lame_rejects_is_resampled_instead_of_failing(
        self, av, tmp_path
    ):
        source = tmp_path / "odd.wav"
        self._wav(source, rate=20000)  # not one of libmp3lame's supported rates
        target = tmp_path / "odd.mp3"

        sidecar_cli._encode(source, target)

        with av.open(str(target)) as container:
            assert container.streams.audio[0].rate == 44100

    def test_the_cli_entry_point_transcodes_and_reports_success(self, tmp_path):
        source = tmp_path / "cli.wav"
        self._wav(source)
        target = tmp_path / "cli.mp3"

        assert sidecar_cli.main(["encode", str(source), str(target)]) == 0
        assert target.stat().st_size > 0


class TestInstallState:
    """The runtime NEVER auto-installs; callers ask, and are told where to enable it."""

    def test_is_installed_reflects_the_manager(self):
        assert AudioSidecar(_FakeManager(installed=True)).is_installed() is True
        assert AudioSidecar(_FakeManager(installed=False)).is_installed() is False

    def test_an_unreachable_envs_dir_reads_as_not_installed(self):
        class _Broken(_FakeManager):
            def is_installed(self, spec):
                raise OSError("no such volume")

        assert AudioSidecar(_Broken()).is_installed() is False

    def test_transcoding_without_the_runtime_names_the_advanced_tab(self):
        sidecar = AudioSidecar(_FakeManager(installed=False))
        with pytest.raises(ProviderError, match="Advanced"):
            sidecar.decode(b"x")
