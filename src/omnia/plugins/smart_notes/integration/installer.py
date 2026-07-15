"""One-click installer for the companion clippers, driven from the Integrations tab.

Two install kinds (see :class:`~omnia.plugins.smart_notes.integration.integrations.Integration`):

* ``"desktop"`` — clone the repo → create a build venv from a real host Python (NOT Anki's frozen
  interpreter) → ``pip install`` its deps + PyInstaller → run ``build.py`` (which builds, ad-hoc
  re-signs, and copies the ``.app`` into /Applications) → open the app (which then prompts for the
  macOS permissions). Replaces the whole manual "make a venv, pip install, python build.py, open,
  grant" flow with one click. It is a genuinely long job (hundreds of MB + a PyInstaller freeze),
  so callers run :meth:`ClipperInstaller.install` OFF the Qt main thread and surface ``progress``.
* ``"web"`` — a browser extension can't be installed programmatically (Chrome blocks it), so this
  clones the repo, reveals the folder, and opens ``chrome://extensions`` for a manual load-unpacked.

The orchestration is pure of Anki/Qt and takes an injected :class:`CommandRunner` + host-python +
clones dir, so it unit-tests with a fake runner (no real git/pip/network).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .integrations import Integration

Progress = Callable[[str], None]

# The macOS app bundle name build.py installs into /Applications (matches build.py's APP_NAME).
_DESKTOP_APP = "Omnia Desktop Clipper.app"


class InstallError(RuntimeError):
    """A step of the install failed (message is safe to show the user)."""


class CommandRunner(Protocol):
    """The subprocess surface the installer needs (injected so tests use a fake)."""

    def run(self, argv: list[str], cwd: Path | None = None) -> None:
        """Run ``argv`` to completion; raise :class:`InstallError` on a non-zero exit."""

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
    ) -> None:
        """Initialise the installer.

        Args:
            clones_dir: Directory to clone the clipper repos into (e.g. ``user_files/clippers``).
            host_python: Path to a real Python 3.10+ to build the venv from, or ``None`` if none
                was found (a desktop install then fails with a clear message).
            runner: The subprocess runner (injected; a fake in tests).
            platform: ``sys.platform`` override (for tests). Defaults to the running platform.
        """
        self._clones_dir = clones_dir
        self._host_python = host_python
        self._runner = runner
        self._platform = platform if platform is not None else sys.platform

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
        self._runner.run([venv_py, "build.py"], cwd=src)  # builds + signs + installs to /Applications
        progress("Opening the app — grant Accessibility + Input Monitoring when asked…")
        self._open_desktop_app(src)

    def _venv_python(self, venv: Path) -> Path:
        if self._platform.startswith("win"):
            return venv / "Scripts" / "python.exe"
        return venv / "bin" / "python"

    def _open_desktop_app(self, src: Path) -> None:
        if self._platform == "darwin":
            self._runner.spawn(["open", "-a", f"/Applications/{_DESKTOP_APP}"])
        elif self._platform.startswith("win"):
            self._runner.spawn(["cmd", "/c", "start", "", str(src / "dist" / "Omnia Desktop Clipper.exe")])
        else:  # linux: launch the built binary from dist/
            self._runner.spawn([str(src / "dist" / "Omnia Desktop Clipper" / "Omnia Desktop Clipper")])

    # -- web: clone -> reveal folder + open chrome://extensions --------------------------------

    def _reveal_web(self, integration: Integration, progress: Progress) -> None:
        src = self._clone_or_update(integration, progress)
        progress("Opening chrome://extensions and revealing the folder to load unpacked…")
        self._reveal(src)
        self._open_chrome_extensions()

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
