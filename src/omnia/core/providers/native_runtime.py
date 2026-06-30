"""Sidecar-venv manager for native-runtime providers (ADR-005).

Some providers need a **native** runtime that cannot be vendored or installed into Anki's
frozen interpreter: ``piper`` needs ``onnxruntime`` (compiled C++), ``viet-tts`` needs
PyTorch. Per ADR-005 these run as out-of-process sidecars inside an add-on-managed,
per-provider virtualenv under ``user_files/native_envs/<provider>/``. The venv's own
interpreter owns the native ABI, so the wheels match by construction and a bad install /
crash stays isolated from Anki.

This module is the :class:`NativeRuntimeManager` seam plus its data + injection points. It
is **pure-Python and headless-importable**: it imports no ``aqt``/``anki`` at module top
(``addon_user_files_dir`` is lazy-imported only inside :func:`default_manager`) and all
subprocess/socket work lives behind the injectable :class:`ProcessRunner`, so the manager
is fully unit-testable with a fake runner — no real venv, pip, or network in tests.

viet-tts (server mode) and piper (cli mode) run through this manager. Installing is an
**explicit, opt-in** user toggle (the GUI in Phase C); the synthesis/run paths never
auto-install — they raise a clear "enable it in Advanced" :class:`ProviderError` when the
runtime is absent. A registry (:data:`NATIVE_RUNTIMES`) lets the GUI enumerate runtimes
grouped by section.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from omnia.core.logging import get_logger
from omnia.core.providers.errors import ProviderError

_logger = get_logger("native_runtime")

# Windows-path seam. ``venv_python`` reads this module-level flag (not ``os.name`` inline) so
# tests can monkeypatch ``native_runtime._IS_WINDOWS`` to exercise BOTH the POSIX
# (``bin/python``) and Windows (``Scripts/python.exe``) layouts on a single host.
_IS_WINDOWS = os.name == "nt"

# Marker file written into a venv once its pip install has fully succeeded; its presence (plus
# the venv python) is what :meth:`NativeRuntimeManager.is_installed` checks, so a half-finished
# install (process killed mid-pip) is correctly treated as not-installed and redone.
_INSTALL_MARKER = ".omnia-installed"


def _tail(output: bytes, max_chars: int = 1200) -> str:
    """Decode the last chunk of captured child output, for surfacing in an error message.

    pip's failure reason (e.g. "Could not find a version that satisfies torch") is in the last
    lines, so we keep the tail (trimmed to ``max_chars``) rather than the whole, often-huge log.
    """
    text = output.decode("utf-8", "replace").strip()
    return ("…" + text[-max_chars:]) if len(text) > max_chars else text


# How long :meth:`NativeRuntimeManager.ensure_running` polls for the sidecar server to start
# listening, and the gap between polls. Mirrors the viet-tts deadline-loop shape.
_SERVER_STARTUP_TIMEOUT = 120.0
_SERVER_POLL_INTERVAL = 0.5

# Bootstrap-interpreter preference. Native runtime deps (PyTorch, onnxruntime) lag the newest
# CPython by a release or two, so a machine whose default ``python3`` is bleeding-edge (no wheels
# yet → ``pip install`` fails) can still install if a slightly older, well-supported minor is on
# PATH. Probe these specific minors before the generic ``python3``/``python``.
_PREFERRED_HOST_PYTHONS = (
    "python3.12",
    "python3.11",
    "python3.13",
    "python3.10",
    "python3.9",
)


@dataclass(frozen=True)
class NativeRuntimeSpec:
    """Declarative description of one native-runtime provider's sidecar venv.

    Pure data — what to install, how to launch, where it listens, and how to present it in
    the GUI. The manager turns this into a venv + a process. ``server_argv`` / ``cli_argv``
    are *templates* whose placeholder tokens are substituted at launch (see the field docs
    and the manager methods).

    Attributes:
        name: Provider id; also the venv directory name (e.g. ``"viettts"``).
        section: GUI grouping key (e.g. ``"tts"`` / ``"llm"``) — which area's Advanced panel
            lists this runtime.
        label: Human-readable name shown in the GUI (e.g. ``"Piper (offline neural, local)"``).
        pip_packages: Packages to ``pip install`` into the venv.
        mode: ``"server"`` (a persistent localhost server) or ``"cli"`` (one-shot command).
        size_hint: Human-readable download size the user is about to fetch (e.g. ``"~50 MB"``,
            ``"~2 GB"``), shown next to the enable toggle.
        server_argv: argv template for ``mode="server"``. Supports the placeholder tokens
            ``{python}`` (the venv python), ``{bin}`` (the venv scripts dir), ``{host}`` and
            ``{port}``, substituted per-launch.
        cli_argv: argv template for ``mode="cli"``. Supports ``{python}`` (the venv python)
            and ``{bin}`` (the venv scripts dir); caller-supplied ``extra_argv`` is appended
            after it (see ``run_in_venv`` / ``run_capture_in_venv``).
        host: Bind/connect host for ``mode="server"``.
        port: Fixed port, or ``0`` to let the manager/caller pick a free one.
    """

    name: str
    pip_packages: tuple[str, ...]
    mode: str
    section: str = ""
    label: str = ""
    size_hint: str = ""
    server_argv: tuple[str, ...] = ()
    cli_argv: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 0

    def __post_init__(self) -> None:
        if self.mode not in ("server", "cli"):
            raise ValueError(
                f"NativeRuntimeSpec {self.name!r}: mode must be 'server' or 'cli', "
                f"got {self.mode!r}"
            )
        if not self.section:
            raise ValueError(
                f"NativeRuntimeSpec {self.name!r}: section must be non-empty"
            )
        if not self.label:
            raise ValueError(
                f"NativeRuntimeSpec {self.name!r}: label must be non-empty"
            )
        if self.mode == "server" and not self.server_argv:
            raise ValueError(
                f"NativeRuntimeSpec {self.name!r}: mode='server' requires server_argv"
            )
        if self.mode == "cli" and not self.cli_argv:
            raise ValueError(
                f"NativeRuntimeSpec {self.name!r}: mode='cli' requires cli_argv"
            )


# Process-wide registry of declared native runtimes, keyed by ``spec.name``. Providers
# register their spec at import time (see viettts.py / piper.py); the GUI enumerates the
# registry to render the Advanced "native runtimes" panel per section.
NATIVE_RUNTIMES: dict[str, NativeRuntimeSpec] = {}


def register_native_runtime(spec: NativeRuntimeSpec) -> NativeRuntimeSpec:
    """Register ``spec`` under ``spec.name`` and return it (so it can be assigned inline).

    Re-registering the identical spec is a no-op (import-order safe). Registering a different
    spec under a name already taken is a programming error and raises.

    Raises:
        ValueError: If ``spec.name`` is already registered with a *different* spec.
    """
    existing = NATIVE_RUNTIMES.get(spec.name)
    if existing is not None and existing != spec:
        raise ValueError(
            f"native runtime {spec.name!r} already registered with a different spec"
        )
    NATIVE_RUNTIMES[spec.name] = spec
    return spec


def native_runtime(name: str) -> NativeRuntimeSpec | None:
    """Return the registered spec for ``name``, or None if unknown."""
    return NATIVE_RUNTIMES.get(name)


def available_native_runtimes() -> list[NativeRuntimeSpec]:
    """Return every registered spec, sorted by name (stable order for the GUI)."""
    return sorted(NATIVE_RUNTIMES.values(), key=lambda spec: spec.name)


def native_runtimes_by_section() -> dict[str, list[NativeRuntimeSpec]]:
    """Return registered specs grouped by ``section`` (each list sorted by name).

    The mapping is sorted by section, then by name within a section, so the GUI renders a
    deterministic layout.
    """
    grouped: dict[str, list[NativeRuntimeSpec]] = {}
    for spec in available_native_runtimes():
        grouped.setdefault(spec.section, []).append(spec)
    return {section: grouped[section] for section in sorted(grouped)}


@runtime_checkable
class ProcessRunner(Protocol):
    """Injected seam over the OS so the manager is unit-testable without real processes.

    A fake implementation lets tests script ``which``/``run``/``popen``/``is_listening``
    return values and assert the exact argv recorded, with no subprocess or socket. The
    concrete :class:`SubprocessRunner` provides the real stdlib-backed behaviour.
    """

    def which(self, exe: str) -> str | None:
        """Resolve ``exe`` to a full path on PATH, or None if not found."""
        ...

    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> int:
        """Run ``argv`` to completion and return its exit code (venv create / pip install).

        ``input`` (if given) is written to the child's stdin (e.g. piper reads text on stdin).
        """
        ...

    def run_capture(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> tuple[int, bytes]:
        """Run ``argv`` to completion, returning ``(exit_code, captured_bytes)``.

        Used by one-shot CLIs that emit their output (e.g. WAV bytes) on stdout. ``input``
        (if given) is written to the child's stdin. ``merge_stderr`` folds the child's stderr
        into the captured stream (for surfacing an install error) — still captured, never
        written to the real stderr (which would trip Anki's crash dialog).
        """
        ...

    def popen(self, argv: Sequence[str]) -> Any:
        """Spawn ``argv`` as a long-lived process.

        Returns a handle exposing ``.poll()``/``.terminate()``/``.wait()``/``.kill()``
        (e.g. :class:`subprocess.Popen`); used for the persistent server.
        """
        ...

    def is_listening(self, host: str, port: int, timeout: float = 1.0) -> bool:
        """Return whether a TCP listener accepts a connection at ``host:port``."""
        ...


class SubprocessRunner:
    """Concrete :class:`ProcessRunner` backed by stdlib ``subprocess``/``shutil``/``socket``.

    Mirrors viet-tts's ``_is_listening``/``_terminate`` logic so the sidecar lifecycle is
    identical to the existing, proven pattern.
    """

    def which(self, exe: str) -> str | None:
        return shutil.which(exe)

    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> int:
        # Silence child output: inside Anki, writing to stderr triggers its crash dialog
        # (see core/logging docstring); progress is surfaced via on_progress, not the pipes.
        completed = subprocess.run(
            list(argv),
            input=input,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode

    def run_capture(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> tuple[int, bytes]:
        # Capture stdout (e.g. WAV bytes); fold stderr into it when merge_stderr (to surface an
        # install error). Either way stderr is NEVER inherited — writing to the real stderr
        # inside Anki trips its crash dialog (see ``run``).
        completed = subprocess.run(
            list(argv),
            input=input,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode, completed.stdout or b""

    def popen(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_listening(self, host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def free_port() -> int:
        """Bind an ephemeral port, then release it, returning the number the OS assigned.

        There is an unavoidable bind/connect race, but it is the standard portable way to
        find a free port and matches how the sidecar would otherwise be started.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def terminate(proc: Any) -> None:
        """Politely terminate ``proc``, escalating to kill if it does not exit in time."""
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


@dataclass
class _Server:
    """A sidecar server this manager started, tracked for reuse and cleanup."""

    spec: NativeRuntimeSpec
    proc: Any
    host: str
    port: int


class NativeRuntimeManager:
    """Create/cache per-provider venvs and run native-runtime sidecars (ADR-005).

    One manager owns the ``native_envs`` root and the set of sidecar servers it has started.
    All OS interaction goes through the injected :class:`ProcessRunner`, so behaviour is
    fully scriptable in tests. Errors are raised as :class:`ProviderError` with actionable,
    provider-named messages.
    """

    def __init__(
        self,
        envs_dir: Path,
        runner: ProcessRunner | None = None,
        *,
        host_python: str | None = None,
    ) -> None:
        """Initialise the manager.

        Args:
            envs_dir: Root directory holding one subdirectory per provider venv.
            runner: Process/socket seam; defaults to a real :class:`SubprocessRunner`.
            host_python: Explicit bootstrap interpreter path; overrides PATH detection
                (mainly for tests).
        """
        self._envs_dir = envs_dir
        self._runner: ProcessRunner = (
            runner if runner is not None else SubprocessRunner()
        )
        self._host_python_override = host_python
        self._servers: dict[str, _Server] = {}

    # -- interpreter / path resolution ----------------------------------------------------
    def host_python(self) -> str | None:
        """Detect a bootstrap interpreter to create the venv (NOT Anki's frozen one).

        Prefers an explicit ``host_python`` override, then probes specific torch-friendly
        minors (see ``_PREFERRED_HOST_PYTHONS``) before the generic ``python3``/``python``, and
        (on Windows) the ``py`` launcher — all via ``runner.which``. Probing versioned minors
        first lets a machine whose default ``python3`` is too new for the runtime's wheels still
        install when an older, supported minor is also on PATH.

        Returns:
            The interpreter path, or None when no usable host Python is on PATH.
        """
        if self._host_python_override:
            return self._host_python_override
        candidates = [*_PREFERRED_HOST_PYTHONS, "python3", "python"]
        if _IS_WINDOWS:
            candidates.append("py")
        for exe in candidates:
            found = self._runner.which(exe)
            if found:
                return found
        return None

    def venv_dir(self, spec: NativeRuntimeSpec) -> Path:
        """Return the venv directory for ``spec`` (``envs_dir / spec.name``)."""
        return self._envs_dir / spec.name

    def venv_python(self, spec: NativeRuntimeSpec) -> Path:
        """Return the venv's python path, cross-platform.

        Reads the module-level ``_IS_WINDOWS`` seam (monkeypatchable in tests) rather than
        ``os.name`` inline, so both layouts are exercisable on one host:
        ``Scripts/python.exe`` on Windows, else ``bin/python``.
        """
        base = self.venv_dir(spec)
        if _IS_WINDOWS:
            return base / "Scripts" / "python.exe"
        return base / "bin" / "python"

    def venv_bin_dir(self, spec: NativeRuntimeSpec) -> Path:
        """Return the venv's console-scripts directory (``Scripts`` on Windows, else ``bin``).

        Reads the module-level ``_IS_WINDOWS`` seam so both layouts are exercisable on one host.
        """
        return self.venv_dir(spec) / ("Scripts" if _IS_WINDOWS else "bin")

    def venv_exe(self, spec: NativeRuntimeSpec, name: str) -> Path:
        """Resolve a console script ``name`` installed in ``spec``'s venv, cross-platform.

        Console scripts are the reliable cross-platform entry point for these packages
        (``viettts``, ``piper``); on Windows they are ``Scripts/<name>.exe``, else ``bin/<name>``.
        """
        exe = f"{name}.exe" if _IS_WINDOWS else name
        return self.venv_bin_dir(spec) / exe

    def _marker_path(self, spec: NativeRuntimeSpec) -> Path:
        return self.venv_dir(spec) / _INSTALL_MARKER

    # -- install --------------------------------------------------------------------------
    def is_installed(self, spec: NativeRuntimeSpec) -> bool:
        """Return whether ``spec``'s venv exists AND its install marker is present."""
        return self.venv_python(spec).exists() and self._marker_path(spec).exists()

    def ensure_installed(
        self,
        spec: NativeRuntimeSpec,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Idempotently create the venv and pip-install ``spec``'s packages.

        No-op if already installed. Otherwise: require a host Python, create the venv,
        ``pip install`` the declared packages, and write the install marker on success.
        ``on_progress`` (if given) receives short human-readable status strings.

        Args:
            spec: The provider runtime to install.
            on_progress: Optional callback for status updates.

        Raises:
            ProviderError: If no host Python is available, or venv creation / pip install
                returns a non-zero exit code.
        """

        def _progress(message: str) -> None:
            _logger.info("[%s] %s", spec.name, message)
            if on_progress is not None:
                on_progress(message)

        if self.is_installed(spec):
            return

        host_python = self.host_python()
        if host_python is None:
            raise ProviderError(
                f"{spec.name}: no host Python found to create its runtime. Install "
                f"Python 3.12 (python.org) and ensure `python3`/`python` is on PATH, then "
                f"try again. (Newer releases such as 3.14 may not have wheels for some "
                f"runtimes yet, e.g. PyTorch.)"
            )

        venv_dir = self.venv_dir(spec)
        _progress(f"creating runtime environment at {venv_dir}")
        code = self._runner.run([host_python, "-m", "venv", str(venv_dir)])
        if code != 0:
            raise ProviderError(
                f"{spec.name}: failed to create its runtime environment "
                f"(`python -m venv` exited {code})."
            )

        venv_python = str(self.venv_python(spec))
        # Best-effort pip upgrade: a fresh venv's pip can be old (e.g. 21.x), which struggles
        # with VCS installs + heavy resolutions; ignore failure (the existing pip may suffice).
        _progress("updating pip")
        self._runner.run([venv_python, "-m", "pip", "install", "--upgrade", "pip"])
        _progress(f"installing {', '.join(spec.pip_packages)} (this may take a while)")
        code, output = self._runner.run_capture(
            [venv_python, "-m", "pip", "install", *spec.pip_packages],
            merge_stderr=True,
        )
        if code != 0:
            tail = _tail(output)
            raise ProviderError(
                f"{spec.name}: failed to install its packages (`pip install` exited {code})."
                + (f"\n{tail}" if tail else "")
            )

        marker = self._marker_path(spec)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
        _progress("runtime ready")

    def uninstall(self, spec: NativeRuntimeSpec) -> None:
        """Stop ``spec``'s sidecar (if this manager started one) and delete its venv.

        Idempotent: a no-op if nothing is tracked and the venv is absent. Backs the GUI's
        "untick → delete the venv immediately" toggle so a disabled runtime frees its disk.
        """
        server = self._servers.pop(spec.name, None)
        if server is not None:
            self._terminate(server.proc)
        shutil.rmtree(self.venv_dir(spec), ignore_errors=True)

    def _require_installed(self, spec: NativeRuntimeSpec) -> None:
        """Raise an actionable error if ``spec`` is not installed (opt-in model, ADR-005).

        The run paths never auto-install: installing is an explicit user toggle (Phase C wires
        it). This keeps the slow, network-heavy first-run install out of the synthesis path.

        Raises:
            ProviderError: If ``spec``'s venv is not installed.
        """
        if not self.is_installed(spec):
            raise ProviderError(
                f"{spec.label} isn't installed — enable it in Smart Notes → Options → "
                f"Advanced (native runtimes)."
            )

    # -- server (mode='server') -----------------------------------------------------------
    def ensure_running(self, spec: NativeRuntimeSpec) -> tuple[str, int]:
        """Ensure ``spec``'s sidecar server is listening; start it if needed.

        Reuses an already-listening server. Otherwise requires the venv to be installed
        (raising if not — installs are an explicit user toggle, never auto-run here), picks a
        port (``spec.port`` or a free one), launches ``server_argv`` (with ``{python}`` /
        ``{bin}`` / ``{host}`` / ``{port}`` substituted), polls until it listens, and tracks
        it for cleanup.

        Returns:
            The ``(host, port)`` the server listens on.

        Raises:
            ProviderError: If ``spec.mode`` is not ``"server"``, the runtime is not installed,
                or the server exits / does not start listening before the timeout.
        """
        if spec.mode != "server":
            raise ProviderError(
                f"{spec.name}: ensure_running requires mode='server', got {spec.mode!r}."
            )

        host = spec.host
        port = spec.port or self._free_port()

        if self._runner.is_listening(host, port):
            return host, port

        self._require_installed(spec)

        argv = self._substitute(
            spec.server_argv,
            {
                "{python}": str(self.venv_python(spec)),
                "{bin}": str(self.venv_bin_dir(spec)),
                "{host}": host,
                "{port}": str(port),
            },
        )
        try:
            proc = self._runner.popen(argv)
        except OSError as exc:
            raise ProviderError(
                f"{spec.name}: failed to start its server: {exc}"
            ) from exc
        self._servers[spec.name] = _Server(spec=spec, proc=proc, host=host, port=port)
        atexit.register(self._terminate, proc)

        deadline = time.monotonic() + _SERVER_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self._runner.is_listening(host, port):
                return host, port
            if proc.poll() is not None:
                raise ProviderError(
                    f"{spec.name}: its server exited during startup "
                    f"(check the install / logs)."
                )
            time.sleep(_SERVER_POLL_INTERVAL)
        raise ProviderError(
            f"{spec.name}: its server did not start listening at {host}:{port} within "
            f"{_SERVER_STARTUP_TIMEOUT:.0f}s."
        )

    # -- one-shot (mode='cli') ------------------------------------------------------------
    def _cli_argv(
        self, spec: NativeRuntimeSpec, extra_argv: Sequence[str]
    ) -> list[str]:
        """Build the full one-shot argv: substituted ``cli_argv`` + the per-call ``extra_argv``.

        Substitutes ``{python}`` (the venv python) and ``{bin}`` (the venv scripts dir) in
        ``cli_argv``, then appends ``extra_argv``. Requires ``mode="cli"`` and an installed
        runtime (the run paths never auto-install — ADR-005 opt-in model).

        Raises:
            ProviderError: If ``spec.mode`` is not ``"cli"``, or the runtime is not installed.
        """
        if spec.mode != "cli":
            raise ProviderError(
                f"{spec.name}: run_in_venv requires mode='cli', got {spec.mode!r}."
            )
        self._require_installed(spec)
        argv = self._substitute(
            spec.cli_argv,
            {
                "{python}": str(self.venv_python(spec)),
                "{bin}": str(self.venv_bin_dir(spec)),
            },
        )
        return [*argv, *extra_argv]

    def run_in_venv(
        self,
        spec: NativeRuntimeSpec,
        extra_argv: Sequence[str],
        *,
        input: bytes | None = None,
    ) -> int:
        """Run ``spec``'s one-shot CLI in its venv and return the exit code.

        The argv contract is ``[*cli_argv-with-tokens-substituted, *extra_argv]``. ``input``
        (if given) is written to the child's stdin.

        Raises:
            ProviderError: If ``spec.mode`` is not ``"cli"``, or the runtime is not installed.
        """
        return self._runner.run(self._cli_argv(spec, extra_argv), input=input)

    def run_capture_in_venv(
        self,
        spec: NativeRuntimeSpec,
        extra_argv: Sequence[str],
        *,
        input: bytes | None = None,
    ) -> tuple[int, bytes]:
        """Run ``spec``'s one-shot CLI in its venv, returning ``(exit_code, stdout_bytes)``.

        Same argv contract as :meth:`run_in_venv`; ``input`` (if given) goes to stdin. Used by
        CLIs (e.g. piper) that emit their output bytes on stdout.

        Raises:
            ProviderError: If ``spec.mode`` is not ``"cli"``, or the runtime is not installed.
        """
        return self._runner.run_capture(self._cli_argv(spec, extra_argv), input=input)

    # -- cleanup --------------------------------------------------------------------------
    def shutdown_all(self) -> None:
        """Terminate every sidecar server this manager started (safe from ``atexit``)."""
        for server in self._servers.values():
            self._terminate(server.proc)
        self._servers.clear()

    # -- helpers --------------------------------------------------------------------------
    def _free_port(self) -> int:
        """Pick a free TCP port via the runner if it can, else the stdlib helper."""
        runner_free = getattr(self._runner, "free_port", None)
        if callable(runner_free):
            return int(runner_free())
        return SubprocessRunner.free_port()

    def _terminate(self, proc: Any) -> None:
        runner_terminate = getattr(self._runner, "terminate", None)
        if callable(runner_terminate):
            runner_terminate(proc)
            return
        SubprocessRunner.terminate(proc)

    @staticmethod
    def _substitute(argv: Sequence[str], replacements: dict[str, str]) -> list[str]:
        """Substitute placeholder tokens (``{python}``, ``{bin}``, …) in an argv template.

        Replacement is by substring so a placeholder embedded in a larger argument is handled
        too — e.g. ``"{bin}/viettts"`` expands to ``"<venv>/bin/viettts"``, while a standalone
        ``"{port}"`` expands as before.
        """
        result: list[str] = []
        for token in argv:
            for placeholder, value in replacements.items():
                token = token.replace(placeholder, value)
            result.append(token)
        return result


# Lazily-built process-wide manager rooted at ``user_files/native_envs``. Not constructed at
# import (would pull in Anki paths); callers go through ``default_manager``.
_default_manager: Optional[NativeRuntimeManager] = None


def default_manager() -> NativeRuntimeManager:
    """Return the process-wide manager rooted at ``<user_files>/native_envs``.

    Lazily built on first call. ``addon_user_files_dir`` is imported here (not at module
    top) so this module stays ``aqt``-free and headless-importable.
    """
    global _default_manager
    if _default_manager is None:
        from omnia import addon_user_files_dir

        envs_dir = addon_user_files_dir() / "native_envs"
        envs_dir.mkdir(parents=True, exist_ok=True)
        _default_manager = NativeRuntimeManager(envs_dir)
    return _default_manager
