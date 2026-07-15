"""One-click installer for the companion clippers, driven from the Integrations tab.

Two install kinds (see :class:`~omnia.plugins.smart_notes.integration.integrations.Integration`):

* ``"desktop"`` — clone the repo → create a build venv from a real host Python (NOT Anki's frozen
  interpreter) → ``pip install`` its deps + PyInstaller → run ``build.py`` (which only builds into
  ``dist/``) → install the built app into a per-platform location (macOS ``/Applications``, Windows
  ``%LOCALAPPDATA%\\Programs``, Linux ``~/.local/share``) → open it (which then prompts for the OS
  permissions). Replaces the whole manual "make a venv, pip install, python build.py, install, open,
  grant" flow with one click. It is a genuinely long job (hundreds of MB + a PyInstaller freeze),
  so callers run :meth:`ClipperInstaller.install` OFF the Qt main thread and surface ``progress``.
* ``"web"`` — a browser extension can't be installed programmatically (Chrome blocks it), so this
  clones the repo, reveals the folder, and opens ``chrome://extensions`` for a manual load-unpacked.

Everything is cross-platform (macOS / Windows / Linux): git + venv + pip + build.py are the same
everywhere, the venv python and install location branch on ``platform``, and the copy uses
:func:`shutil.copytree`. Reinstalling later pulls + rebuilds; :meth:`ClipperInstaller.status`
compares the installed commit (recorded in an ``.omnia-installed`` marker) against the remote
``main`` HEAD so the button can offer Install / Upgrade / Up-to-date.

The orchestration is pure of Anki/Qt and takes an injected :class:`CommandRunner` + host-python +
clones dir (and an optional ``install_root``), so it unit-tests with a fake runner (no real
git/pip/network) and installs under a temp dir instead of the real ``/Applications``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .integrations import Integration

Progress = Callable[[str], None]

# PyInstaller's build output name (matches the clipper's build.py APP_NAME). On macOS build.py
# produces ``dist/<name>.app`` (a bundle); on Windows/Linux ``dist/<name>/`` (a onedir folder
# whose binary is ``<name>.exe`` / ``<name>``).
_DESKTOP_APP_NAME = "Omnia Desktop Clipper"
_DESKTOP_APP = f"{_DESKTOP_APP_NAME}.app"
# Marker file written into a clone after a successful install, holding the installed commit SHA.
# status() compares it against the remote main HEAD to offer Install / Upgrade / Up-to-date
# (mirrors the ``.omnia-installed`` marker the native-runtime manager uses for its venvs).
_MARKER = ".omnia-installed"


class InstallError(RuntimeError):
    """A step of the install failed (message is safe to show the user)."""


class CommandRunner(Protocol):
    """The subprocess surface the installer needs (injected so tests use a fake)."""

    def run(self, argv: list[str], cwd: Path | None = None) -> None:
        """Run ``argv`` to completion; raise :class:`InstallError` on a non-zero exit."""

    def run_capture(self, argv: list[str], cwd: Path | None = None) -> str:
        """Run ``argv`` and return its stdout; raise :class:`InstallError` on a non-zero exit.

        For short read-only queries (``git rev-parse`` / ``git ls-remote``) whose output the
        installer needs, unlike :meth:`run` which only surfaces output on failure.
        """

    def spawn(self, argv: list[str]) -> None:
        """Fire-and-forget launch (opening an app / a URL / a Finder reveal)."""


class SubprocessCommandRunner:
    """The real :class:`CommandRunner`: subprocess with output captured (never Anki's stderr).

    Output is captured to a pipe (writing to Anki's stderr triggers its crash dialog) and the
    tail is surfaced in :class:`InstallError` so a failed clone/pip/build is diagnosable.
    """

    def run(self, argv: list[str], cwd: Path | None = None) -> None:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=1800,  # 30-min ceiling; a first-time build + downloads can be slow
            )
        except FileNotFoundError as exc:
            raise InstallError(f"Command not found: {argv[0]!r}. Is it installed?") from exc
        except subprocess.TimeoutExpired as exc:
            raise InstallError(f"`{argv[0]} …` timed out.") from exc
        if completed.returncode != 0:
            tail = (completed.stdout or "").strip()[-1000:]
            raise InstallError(
                f"`{' '.join(argv[:3])} …` failed (exit {completed.returncode}).\n{tail}"
            )

    def run_capture(self, argv: list[str], cwd: Path | None = None) -> str:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,  # short read-only git queries
            )
        except FileNotFoundError as exc:
            raise InstallError(f"Command not found: {argv[0]!r}. Is it installed?") from exc
        except subprocess.TimeoutExpired as exc:
            raise InstallError(f"`{argv[0]} …` timed out.") from exc
        if completed.returncode != 0:
            raise InstallError(f"`{' '.join(argv[:3])} …` failed (exit {completed.returncode}).")
        return completed.stdout or ""

    def spawn(self, argv: list[str]) -> None:
        try:
            subprocess.Popen(argv)  # fixed argv, no shell
        except OSError as exc:
            raise InstallError(f"Could not launch {argv[0]!r}: {exc}") from exc


class ClipperInstaller:
    """Clones + builds + installs (desktop) or reveals (web) a clipper integration."""

    def __init__(
        self,
        *,
        clones_dir: Path,
        host_python: str | None,
        runner: CommandRunner,
        platform: str | None = None,
        install_root: Path | None = None,
    ) -> None:
        """Initialise the installer.

        Args:
            clones_dir: Directory to clone the clipper repos into (e.g. ``user_files/clippers``).
            host_python: Path to a real Python 3.10+ to build the venv from, or ``None`` if none
                was found (a desktop install then fails with a clear message).
            runner: The subprocess runner (injected; a fake in tests).
            platform: ``sys.platform`` override (for tests). Defaults to the running platform.
            install_root: Override the directory the built app is installed under (for tests, so
                they never touch the real ``/Applications``). ``None`` uses the per-platform
                default(s) — see :meth:`_app_dest_dirs`.
        """
        self._clones_dir = clones_dir
        self._host_python = host_python
        self._runner = runner
        self._platform = platform if platform is not None else sys.platform
        self._install_root = install_root

    def install(self, integration: Integration, progress: Progress) -> None:
        """Install ``integration`` per its ``install_kind`` (progress reported via ``progress``)."""
        if not integration.repo_url or not integration.install_kind:
            raise InstallError(f"{integration.name} can't be installed automatically.")
        if integration.install_kind == "desktop":
            self._install_desktop(integration, progress)
        elif integration.install_kind == "web":
            self._reveal_web(integration, progress)
        else:
            raise InstallError(f"Unknown install kind {integration.install_kind!r}.")

    # -- desktop: clone -> venv -> pip -> build -> open ----------------------------------------

    def _install_desktop(self, integration: Integration, progress: Progress) -> None:
        src = self._clone_or_update(integration, progress)
        if not self._host_python:
            raise InstallError(
                "No Python 3.10+ was found to build the app. Install Python (e.g. 3.11 or 3.12) "
                "from python.org, then click Install again."
            )
        venv = src / ".venv-build"
        venv_py = str(self._venv_python(venv))
        progress("Creating the build environment…")
        self._runner.run([self._host_python, "-m", "venv", str(venv)])
        progress("Installing dependencies (a few hundred MB — first run only)…")
        self._runner.run([venv_py, "-m", "pip", "install", "--upgrade", "pip"])
        self._runner.run(
            [venv_py, "-m", "pip", "install", "-r", "requirements.txt", "pyinstaller"],
            cwd=src,
        )
        progress("Building the app (this can take several minutes)…")
        self._runner.run([venv_py, "build.py"], cwd=src)  # produces dist/<app>
        # build.py only builds into dist/; the installer owns placing the app where it can be
        # launched (and knows the exact path to open — an earlier bug opened a hardcoded
        # /Applications path the build had never created, so nothing launched).
        launch_path = self._install_bundle(src, progress)
        progress("Opening the app — grant the permissions it asks for…")
        self._open_desktop_app(launch_path)
        self._write_marker(src)

    def _venv_python(self, venv: Path) -> Path:
        if self._platform.startswith("win"):
            return venv / "Scripts" / "python.exe"
        return venv / "bin" / "python"

    def _app_dest_dirs(self) -> list[Path]:
        """The ordered candidate parent directories to install the built app under, per platform.

        macOS tries ``/Applications`` then falls back to ``~/Applications`` (the latter needs no
        admin rights). Windows uses ``%LOCALAPPDATA%\\Programs``; Linux ``~/.local/share`` — both
        per-user, so no elevation is needed. ``install_root`` overrides all of this in tests.
        """
        if self._install_root is not None:
            return [self._install_root]
        home = Path.home()
        if self._platform == "darwin":
            return [Path("/Applications"), home / "Applications"]
        if self._platform.startswith("win"):
            local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
            return [Path(local) / "Programs"]
        return [home / ".local" / "share"]  # linux

    def _install_bundle(self, src: Path, progress: Progress) -> Path:
        """Copy the freshly built app out of the clone's ``dist/`` to a per-platform install
        location and return the path to launch.

        ``build.py`` only builds into ``dist/`` (buried in Anki's ``user_files``); the installer
        owns placing it somewhere launchable — and knowing that exact path to open (an earlier bug
        opened a hardcoded ``/Applications`` path the build had never created, so nothing launched).
        Uses :func:`shutil.copytree` (``symlinks=True`` to keep a ``.app``'s framework symlinks) so
        the copy is identical on macOS, Windows and Linux.
        """
        is_mac = self._platform == "darwin"
        name = _DESKTOP_APP if is_mac else _DESKTOP_APP_NAME
        source = src / "dist" / name
        if not source.exists():
            raise InstallError(f"The build finished but produced no app at {source}.")
        last_err = ""
        for base in self._app_dest_dirs():
            dest = base / name
            progress(f"Installing into {base}…")
            try:
                base.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(source, dest, symlinks=True)
            except (OSError, shutil.Error) as exc:  # not writable → try the next candidate
                last_err = f"{base}: {exc}"
                continue
            # macOS launches the .app bundle itself; Windows/Linux launch the inner binary.
            return dest if is_mac else dest / self._desktop_exe_name()
        raise InstallError(f"Built the app but could not install it. {last_err}")

    def _desktop_exe_name(self) -> str:
        return f"{_DESKTOP_APP_NAME}.exe" if self._platform.startswith("win") else _DESKTOP_APP_NAME

    def _open_desktop_app(self, launch_path: Path) -> None:
        if self._platform == "darwin":
            self._runner.spawn(["open", str(launch_path)])  # open the installed .app bundle
        elif self._platform.startswith("win"):
            self._runner.spawn(["cmd", "/c", "start", "", str(launch_path)])
        else:  # linux: launch the built binary directly
            self._runner.spawn([str(launch_path)])

    # -- web: clone -> reveal folder + open chrome://extensions --------------------------------

    def _reveal_web(self, integration: Integration, progress: Progress) -> None:
        src = self._clone_or_update(integration, progress)
        progress("Opening chrome://extensions and revealing the folder to load unpacked…")
        self._reveal(src)
        self._open_chrome_extensions()
        self._write_marker(src)

    def _reveal(self, path: Path) -> None:
        if self._platform == "darwin":
            self._runner.spawn(["open", "-R", str(path)])
        elif self._platform.startswith("win"):
            self._runner.spawn(["explorer", str(path)])
        else:
            self._runner.spawn(["xdg-open", str(path)])

    def _open_chrome_extensions(self) -> None:
        url = "chrome://extensions/"
        if self._platform == "darwin":
            self._runner.spawn(["open", "-a", "Google Chrome", url])
        elif self._platform.startswith("win"):
            self._runner.spawn(["cmd", "/c", "start", "chrome", url])
        else:
            self._runner.spawn(["google-chrome", url])

    # -- install state (Install / Upgrade / Up-to-date button) --------------------------------

    def status(self, integration: Integration) -> dict[str, bool]:
        """Return ``{"installed", "upgrade"}`` for ``integration``'s Integrations-tab button.

        * ``installed`` — a prior install wrote the commit marker into the clone.
        * ``upgrade`` — the integration's remote ``main`` HEAD differs from the installed commit,
          i.e. there are new commits to pull + rebuild.

        A network failure on the remote lookup is treated as "no upgrade" so a flaky connection
        never shows a false Upgrade prompt (the button just stays "Up to date").
        """
        if not integration.repo_url or not integration.install_kind:
            return {"installed": False, "upgrade": False}
        marker = self._clones_dir / integration.key / _MARKER
        if not marker.is_file():
            return {"installed": False, "upgrade": False}
        installed = marker.read_text(encoding="utf-8").strip()
        remote = self._remote_head(integration)
        return {"installed": True, "upgrade": bool(remote) and remote != installed}

    def _write_marker(self, src: Path) -> None:
        """Record the just-installed commit SHA so :meth:`status` can detect a later upgrade.

        Best-effort: if ``rev-parse`` fails the marker is simply not written (the button then
        shows "Install" again rather than crashing the install).
        """
        if not src.is_dir():
            return
        try:
            sha = self._runner.run_capture(["git", "-C", str(src), "rev-parse", "HEAD"]).strip()
        except InstallError:
            return
        if sha:
            (src / _MARKER).write_text(sha, encoding="utf-8")

    def _remote_head(self, integration: Integration) -> str:
        """The remote ``main`` HEAD SHA, or ``""`` if it can't be reached."""
        try:
            out = self._runner.run_capture(
                ["git", "ls-remote", integration.repo_url, "refs/heads/main"]
            )
        except InstallError:
            return ""
        first = out.strip().split("\n", 1)[0] if out.strip() else ""
        return first.split()[0] if first else ""

    # -- shared: git clone / pull -------------------------------------------------------------

    def _clone_or_update(self, integration: Integration, progress: Progress) -> Path:
        dest = self._clones_dir / integration.key
        if (dest / ".git").is_dir():
            progress(f"Updating {integration.name}…")
            self._runner.run(["git", "-C", str(dest), "pull", "--ff-only"])
        else:
            progress(f"Cloning {integration.name}…")
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._runner.run(
                ["git", "clone", "--depth", "1", integration.repo_url, str(dest)]
            )
        return dest
