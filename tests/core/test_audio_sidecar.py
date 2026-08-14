"""Tests for the ``audio`` native runtime wrapper (``core/audio/sidecar.py``).

The runtime itself (PyAV + FFmpeg) is a compiled wheel that only ever exists inside the
managed venv, so nothing here runs a real transcode: the
:class:`~omnia.core.providers.native_runtime.NativeRuntimeManager` is faked, and what is
actually asserted is the CONTRACT around it — the spec the Advanced tab renders, the file-based
argv (never stdin/stdout, which Windows would corrupt), and the actionable error a caller gets
when the runtime is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnia.core.audio.sidecar import SPEC, AudioSidecar
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
