"""Tests for the NativeRuntimeManager sidecar-venv seam (ADR-005, Phase A).

Fully offline: a ``_FakeProcessRunner`` records and scripts ``run``/``popen``/``which``/
``is_listening`` so no real subprocess, venv, pip, or socket is touched. The Windows path
layout is exercised by monkeypatching the module-level ``_IS_WINDOWS`` seam, so both the
POSIX (``bin/python``) and Windows (``Scripts/python.exe``) branches are covered on one host.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnia.core.providers import native_runtime
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.native_runtime import (
    NativeRuntimeManager,
    NativeRuntimeSpec,
    available_native_runtimes,
)
from omnia.core.providers.native_runtime import native_runtime as lookup_native_runtime
from omnia.core.providers.native_runtime import (
    native_runtimes_by_section,
    register_native_runtime,
)


class _FakeProc:
    """Minimal process handle: scriptable ``poll`` sequence + recorded lifecycle calls."""

    def __init__(self, poll_results: Sequence[int | None] | None = None) -> None:
        # Each poll() pops the next result; None = still running. Default: always running.
        self._poll_results = list(poll_results or [])
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        if self._poll_results:
            return self._poll_results.pop(0)
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


class _FakeProcessRunner:
    """Records calls and returns scripted values; no real OS interaction."""

    def __init__(
        self,
        *,
        which_map: dict[str, str] | None = None,
        run_codes: Sequence[int] | None = None,
        listening: Sequence[bool] | None = None,
        proc: _FakeProc | None = None,
        free_port: int | None = None,
    ) -> None:
        self._which_map = which_map or {}
        self._run_codes = list(run_codes or [])
        self._listening = list(listening or [])
        self._proc = proc or _FakeProc()
        self._free_port = free_port
        self.run_calls: list[list[str]] = []
        self.run_inputs: list[bytes | None] = []
        self.popen_calls: list[list[str]] = []
        self.which_calls: list[str] = []
        self.is_listening_calls: list[tuple[str, int]] = []
        self.terminated: list[Any] = []
        self.capture_stdout: bytes = b""

    def which(self, exe: str) -> str | None:
        self.which_calls.append(exe)
        return self._which_map.get(exe)

    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> int:
        self.run_calls.append(list(argv))
        self.run_inputs.append(input)
        code = self._run_codes.pop(0) if self._run_codes else 0
        # Mimic `python -m venv <dir>` materializing the venv interpreter on success, so
        # is_installed (which checks the python exists) reflects a real install.
        if code == 0 and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "venv":
            venv_python = Path(argv[3]) / (
                "Scripts/python.exe" if native_runtime._IS_WINDOWS else "bin/python"
            )
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("#!python", encoding="utf-8")
        return code

    def run_capture(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> tuple[int, bytes]:
        code = self.run(argv, input=input, timeout=timeout)
        return code, self.capture_stdout

    def popen(self, argv: Sequence[str]) -> _FakeProc:
        self.popen_calls.append(list(argv))
        return self._proc

    def is_listening(self, host: str, port: int, timeout: float = 1.0) -> bool:
        self.is_listening_calls.append((host, port))
        return self._listening.pop(0) if self._listening else False

    def free_port(self) -> int:
        assert self._free_port is not None, "test did not script a free_port"
        return self._free_port

    def terminate(self, proc: Any) -> None:
        self.terminated.append(proc)


def _server_spec(**overrides: Any) -> NativeRuntimeSpec:
    kwargs: dict[str, Any] = {
        "name": "viettts",
        "section": "tts",
        "label": "VietTTS (Vietnamese, local)",
        "size_hint": "~2 GB",
        "pip_packages": ("viet-tts",),
        "mode": "server",
        "server_argv": (
            "{python}",
            "-m",
            "viettts",
            "--host",
            "{host}",
            "--port",
            "{port}",
        ),
        "port": 8298,
    }
    kwargs.update(overrides)
    return NativeRuntimeSpec(**kwargs)


def _cli_spec(**overrides: Any) -> NativeRuntimeSpec:
    kwargs: dict[str, Any] = {
        "name": "piper",
        "section": "tts",
        "label": "Piper (offline neural, local)",
        "size_hint": "~50 MB",
        "pip_packages": ("piper-tts", "onnxruntime"),
        "mode": "cli",
        "cli_argv": ("{python}", "-m", "piper"),
    }
    kwargs.update(overrides)
    return NativeRuntimeSpec(**kwargs)


def _mark_installed(manager: NativeRuntimeManager, spec: NativeRuntimeSpec) -> None:
    """Simulate a completed install: create the venv python + the marker file on disk."""
    venv_python = manager.venv_python(spec)
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!python", encoding="utf-8")
    (manager.venv_dir(spec) / native_runtime._INSTALL_MARKER).write_text("ok")


class TestNativeRuntimeSpec:
    def test_valid_server_spec(self) -> None:
        spec = _server_spec()
        assert spec.mode == "server"
        assert spec.server_argv[0] == "{python}"

    def test_valid_cli_spec(self) -> None:
        spec = _cli_spec()
        assert spec.mode == "cli"
        assert spec.cli_argv[0] == "{python}"

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            NativeRuntimeSpec(
                name="x", pip_packages=("p",), mode="daemon", section="tts", label="X"
            )

    def test_empty_section_raises(self) -> None:
        with pytest.raises(ValueError, match="section must be"):
            NativeRuntimeSpec(
                name="x",
                pip_packages=("p",),
                mode="cli",
                cli_argv=("{python}",),
                label="X",
            )

    def test_empty_label_raises(self) -> None:
        with pytest.raises(ValueError, match="label must be"):
            NativeRuntimeSpec(
                name="x",
                pip_packages=("p",),
                mode="cli",
                cli_argv=("{python}",),
                section="tts",
            )

    def test_size_hint_is_carried(self) -> None:
        assert _server_spec().size_hint == "~2 GB"

    def test_server_mode_without_argv_raises(self) -> None:
        with pytest.raises(ValueError, match="requires server_argv"):
            NativeRuntimeSpec(
                name="x", pip_packages=("p",), mode="server", section="tts", label="X"
            )

    def test_cli_mode_without_argv_raises(self) -> None:
        with pytest.raises(ValueError, match="requires cli_argv"):
            NativeRuntimeSpec(
                name="x", pip_packages=("p",), mode="cli", section="tts", label="X"
            )


class TestHostPython:
    def test_explicit_override_wins(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={"python3": "/usr/bin/python3"})
        manager = NativeRuntimeManager(
            tmp_path, runner, host_python="/opt/py/bin/python"
        )
        assert manager.host_python() == "/opt/py/bin/python"
        assert runner.which_calls == []  # override short-circuits PATH probing

    def test_falls_back_to_python3(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={"python3": "/usr/bin/python3"})
        manager = NativeRuntimeManager(tmp_path, runner)
        assert manager.host_python() == "/usr/bin/python3"

    def test_prefers_versioned_minor_over_generic_python3(self, tmp_path: Path) -> None:
        # A torch-friendly minor on PATH wins over the generic python3 (which may be too new
        # for the runtime's wheels).
        runner = _FakeProcessRunner(
            which_map={
                "python3.12": "/usr/bin/python3.12",
                "python3": "/usr/bin/python3",
            }
        )
        manager = NativeRuntimeManager(tmp_path, runner)
        assert manager.host_python() == "/usr/bin/python3.12"

    def test_falls_back_to_python_when_no_python3(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={"python": "/usr/bin/python"})
        manager = NativeRuntimeManager(tmp_path, runner)
        assert manager.host_python() == "/usr/bin/python"

    def test_probes_py_launcher_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_runtime, "_IS_WINDOWS", True)
        runner = _FakeProcessRunner(which_map={"py": r"C:\Windows\py.exe"})
        manager = NativeRuntimeManager(tmp_path, runner)
        assert manager.host_python() == r"C:\Windows\py.exe"
        assert "py" in runner.which_calls

    def test_returns_none_when_no_python(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={})
        manager = NativeRuntimeManager(tmp_path, runner)
        assert manager.host_python() is None


class TestVenvPython:
    def test_posix_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_runtime, "_IS_WINDOWS", False)
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        spec = _server_spec()
        assert manager.venv_python(spec) == tmp_path / "viettts" / "bin" / "python"

    def test_windows_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_runtime, "_IS_WINDOWS", True)
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        spec = _server_spec()
        assert (
            manager.venv_python(spec) == tmp_path / "viettts" / "Scripts" / "python.exe"
        )


class TestEnsureInstalled:
    def test_idempotent_when_marker_present(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={"python3": "/usr/bin/python3"})
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)

        manager.ensure_installed(spec)

        assert runner.run_calls == []  # nothing run when already installed

    def test_creates_venv_then_pip_installs_in_order(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(
            which_map={"python3": "/usr/bin/python3"}, run_codes=[0, 0]
        )
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        progress: list[str] = []

        manager.ensure_installed(spec, on_progress=progress.append)

        venv_python = str(manager.venv_python(spec))
        assert runner.run_calls[0] == [
            "/usr/bin/python3",
            "-m",
            "venv",
            str(manager.venv_dir(spec)),
        ]
        assert runner.run_calls[1] == [
            venv_python,
            "-m",
            "pip",
            "install",
            "viet-tts",
        ]
        assert manager.is_installed(spec)  # marker written on success
        assert progress  # progress reported around the steps

    def test_raises_when_no_host_python(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={})
        manager = NativeRuntimeManager(tmp_path, runner)
        with pytest.raises(ProviderError, match="Install Python 3"):
            manager.ensure_installed(_server_spec())

    def test_raises_when_venv_create_fails(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(
            which_map={"python3": "/usr/bin/python3"}, run_codes=[1]
        )
        manager = NativeRuntimeManager(tmp_path, runner)
        with pytest.raises(ProviderError, match="venv"):
            manager.ensure_installed(_server_spec())

    def test_raises_when_pip_install_fails(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(
            which_map={"python3": "/usr/bin/python3"}, run_codes=[0, 1]
        )
        # pip's real error (captured via merge_stderr) is surfaced in the message so the user
        # sees WHY — e.g. no matching wheel for their Python.
        runner.capture_stdout = b"ERROR: Could not find a version that satisfies torch"
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        with pytest.raises(ProviderError, match="satisfies torch") as exc:
            manager.ensure_installed(spec)
        assert "pip install" in str(exc.value)
        assert not manager.is_installed(spec)  # no marker on failed install


class TestEnsureRunning:
    def test_returns_when_already_listening(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(listening=[True])
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()

        host, port = manager.ensure_running(spec)

        assert (host, port) == ("127.0.0.1", 8298)
        assert runner.popen_calls == []  # reuse, no spawn

    def test_popens_then_polls_when_installed(self, tmp_path: Path) -> None:
        proc = _FakeProc()
        runner = _FakeProcessRunner(
            listening=[False, True],  # not up at check, then up after popen
            proc=proc,
        )
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)

        host, port = manager.ensure_running(spec)

        assert (host, port) == ("127.0.0.1", 8298)
        assert runner.run_calls == []  # never auto-installs from the run path
        venv_python = str(manager.venv_python(spec))
        assert runner.popen_calls[0] == [
            venv_python,
            "-m",
            "viettts",
            "--host",
            "127.0.0.1",
            "--port",
            "8298",
        ]

    def test_raises_when_not_installed(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(
            which_map={"python3": "/usr/bin/python3"}, listening=[False]
        )
        manager = NativeRuntimeManager(tmp_path, runner)

        with pytest.raises(ProviderError, match="isn't installed"):
            manager.ensure_running(_server_spec())

        assert runner.run_calls == []  # no venv/pip ran
        assert runner.popen_calls == []  # no server spawned

    def test_picks_free_port_when_spec_port_zero(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(free_port=54321, listening=[True])
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec(port=0)

        _host, port = manager.ensure_running(spec)

        assert port == 54321
        assert runner.is_listening_calls[0] == ("127.0.0.1", 54321)

    def test_substitutes_bin_token_in_server_argv(self, tmp_path: Path) -> None:
        proc = _FakeProc()
        runner = _FakeProcessRunner(listening=[False, True], proc=proc)
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec(server_argv=("{bin}/viettts", "server", "--port", "{port}"))
        _mark_installed(manager, spec)

        manager.ensure_running(spec)

        bin_dir = str(manager.venv_bin_dir(spec))
        assert runner.popen_calls[0] == [
            f"{bin_dir}/viettts",
            "server",
            "--port",
            "8298",
        ]

    def test_raises_on_startup_when_proc_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_runtime.time, "sleep", lambda _s: None)
        proc = _FakeProc(poll_results=[1])  # exits during startup
        runner = _FakeProcessRunner(listening=[False, False], proc=proc)
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)
        with pytest.raises(ProviderError, match="exited during startup"):
            manager.ensure_running(spec)

    def test_raises_on_startup_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_runtime, "_SERVER_STARTUP_TIMEOUT", 0.0)
        proc = _FakeProc()  # stays running, never listens
        runner = _FakeProcessRunner(listening=[False], proc=proc)
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)
        with pytest.raises(ProviderError, match="did not start listening"):
            manager.ensure_running(spec)

    def test_rejects_cli_spec(self, tmp_path: Path) -> None:
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        with pytest.raises(ProviderError, match="requires mode='server'"):
            manager.ensure_running(_cli_spec())


class TestRunInVenv:
    def test_runs_substituted_argv_when_installed(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(run_codes=[0])
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _cli_spec()
        _mark_installed(manager, spec)

        code = manager.run_in_venv(spec, ["--text", "hello"], input=b"hi")

        assert code == 0
        venv_python = str(manager.venv_python(spec))
        # The CLI invocation: substituted cli_argv + extra_argv; stdin forwarded.
        assert runner.run_calls[-1] == [
            venv_python,
            "-m",
            "piper",
            "--text",
            "hello",
        ]
        assert runner.run_inputs[-1] == b"hi"

    def test_raises_when_not_installed(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(which_map={"python3": "/usr/bin/python3"})
        manager = NativeRuntimeManager(tmp_path, runner)
        with pytest.raises(ProviderError, match="isn't installed"):
            manager.run_in_venv(_cli_spec(), ["x"])
        assert runner.run_calls == []  # no auto-install, no run

    def test_run_capture_returns_stdout_bytes(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(run_codes=[0])
        runner.capture_stdout = b"WAVDATA"
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _cli_spec()
        _mark_installed(manager, spec)

        code, out = manager.run_capture_in_venv(spec, ["-f", "-"], input=b"hello")

        assert code == 0
        assert out == b"WAVDATA"
        assert runner.run_inputs[-1] == b"hello"

    def test_substitutes_bin_token_in_cli_argv(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner(run_codes=[0])
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _cli_spec(cli_argv=("{bin}/piper",))
        _mark_installed(manager, spec)

        manager.run_in_venv(spec, ["-m", "model.onnx"])

        bin_dir = str(manager.venv_bin_dir(spec))
        assert runner.run_calls[-1] == [f"{bin_dir}/piper", "-m", "model.onnx"]

    def test_rejects_server_spec(self, tmp_path: Path) -> None:
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        with pytest.raises(ProviderError, match="requires mode='cli'"):
            manager.run_in_venv(_server_spec(), [])


class TestShutdownAll:
    def test_terminates_tracked_servers(self, tmp_path: Path) -> None:
        proc = _FakeProc()
        runner = _FakeProcessRunner(listening=[False, True], proc=proc)
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)
        manager.ensure_running(spec)

        manager.shutdown_all()

        assert runner.terminated == [proc]

    def test_safe_when_no_servers(self, tmp_path: Path) -> None:
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        manager.shutdown_all()  # no error


class TestUninstall:
    def test_stops_tracked_server_and_removes_venv(self, tmp_path: Path) -> None:
        proc = _FakeProc()
        runner = _FakeProcessRunner(listening=[False, True], proc=proc)
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _server_spec()
        _mark_installed(manager, spec)
        manager.ensure_running(spec)  # now tracked
        assert manager.is_installed(spec)

        manager.uninstall(spec)

        assert runner.terminated == [proc]  # the tracked server was stopped
        assert not manager.venv_dir(spec).exists()  # venv deleted
        assert not manager.is_installed(spec)

    def test_idempotent_when_absent(self, tmp_path: Path) -> None:
        manager = NativeRuntimeManager(tmp_path, _FakeProcessRunner())
        manager.uninstall(_server_spec())  # no tracked server, no venv → no error

    def test_removes_venv_without_tracked_server(self, tmp_path: Path) -> None:
        runner = _FakeProcessRunner()
        manager = NativeRuntimeManager(tmp_path, runner)
        spec = _cli_spec()
        _mark_installed(manager, spec)

        manager.uninstall(spec)

        assert runner.terminated == []  # nothing was running
        assert not manager.venv_dir(spec).exists()


class TestRegistry:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch: pytest.MonkeyPatch):
        # Each test gets a fresh registry so order / the real provider registrations don't leak.
        monkeypatch.setattr(native_runtime, "NATIVE_RUNTIMES", {})

    def test_register_and_lookup(self) -> None:
        spec = _server_spec()
        assert register_native_runtime(spec) is spec
        assert lookup_native_runtime("viettts") == spec

    def test_lookup_unknown_returns_none(self) -> None:
        assert lookup_native_runtime("nope") is None

    def test_reregister_identical_spec_is_noop(self) -> None:
        register_native_runtime(_server_spec())
        register_native_runtime(_server_spec())  # same spec → no error
        assert len(available_native_runtimes()) == 1

    def test_reregister_different_spec_raises(self) -> None:
        register_native_runtime(_server_spec(port=8298))
        with pytest.raises(ValueError, match="already registered"):
            register_native_runtime(_server_spec(port=9000))

    def test_available_is_sorted_by_name(self) -> None:
        register_native_runtime(_server_spec())  # "viettts"
        register_native_runtime(_cli_spec())  # "piper"
        names = [spec.name for spec in available_native_runtimes()]
        assert names == ["piper", "viettts"]

    def test_by_section_groups_and_sorts(self) -> None:
        register_native_runtime(_server_spec())  # tts / viettts
        register_native_runtime(_cli_spec())  # tts / piper
        register_native_runtime(
            _cli_spec(name="llmthing", section="llm", label="LLM thing")
        )
        grouped = native_runtimes_by_section()
        assert list(grouped) == ["llm", "tts"]  # sections sorted
        assert [s.name for s in grouped["tts"]] == ["piper", "viettts"]
        assert [s.name for s in grouped["llm"]] == ["llmthing"]
