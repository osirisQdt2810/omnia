"""One-click installer for the companion clippers, driven from the Integrations tab.

Two install kinds (see :class:`~omnia.plugins.smart_notes.integration.integrations.Integration`):

* ``"desktop"`` — clone the repo → create a build venv from a real host Python (NOT Anki's frozen
  interpreter) → ``pip install`` its deps + PyInstaller → run ``build.py --no-install`` (which builds
  into ``dist/`` and, on macOS, ad-hoc-signs the bundle with the STABLE bundle-identifier
  requirement, but does NOT install) → install the built app into a per-platform location (macOS
  ``/Applications``, Windows ``%LOCALAPPDATA%\\Programs``, Linux ``~/.local/share``) → open it (which
  then prompts for the OS permissions). Replaces the whole manual "make a venv, pip install, python
  build.py, install, open, grant" flow with one click. It is a genuinely long job (hundreds of MB + a
  PyInstaller freeze), so callers run :meth:`install` OFF the Qt main thread and surface ``progress``.
* ``"web"`` — a browser extension can't be installed programmatically (Chrome blocks it), so this
  clones the repo, puts the folder on the clipboard, and opens a local finish-install page in the
  Chrome profile the user actually uses. Chrome will not open ``chrome://extensions`` from the
  command line either (it drops the URL), which is why that page exists.

Everything is cross-platform (macOS / Windows / Linux): git + venv + pip + build.py are the same
everywhere, and the venv python and install location branch on ``platform``. The installer is the
single owner of installation (``build.py`` is run with ``--no-install``) so it controls both the
location it opens and the copy tool: macOS copies with ``ditto`` to preserve the code signature
``build.py`` applied (that signature is what keeps Accessibility / Input-Monitoring grants across
rebuilds); Windows/Linux copy with :func:`shutil.copytree`. Reinstalling later pulls + rebuilds;
:meth:`status` compares the installed commit (recorded in an ``.omnia-installed`` marker) against
the remote ``main`` HEAD so the button can offer Install / Upgrade / Up-to-date.

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
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.integration.browser import ChromeProfile

from .integrations import Integration

Progress = Callable[[str], None]

# PyInstaller's build output name (matches the clipper's build.py APP_NAME). On macOS build.py
# produces ``dist/<name>.app`` (a bundle); on Windows/Linux ``dist/<name>/`` (a onedir folder
# whose binary is ``<name>.exe`` / ``<name>``).
_DESKTOP_APP_NAME = "Omnia Desktop Clipper"
_DESKTOP_APP = f"{_DESKTOP_APP_NAME}.app"
# The extension page Reload opens. It carries ?omnia-reload=1, which the clipper answers by
# reloading itself and reopening Settings (see that repo's options.js / background.js).
_WEB_OPTIONS_PAGE = "src/options.html"
# Marker file written into a clone after a successful install, holding the installed commit SHA.
# status() compares it against the remote main HEAD to offer Install / Upgrade / Up-to-date
# (mirrors the ``.omnia-installed`` marker the native-runtime manager uses for its venvs).
_MARKER = ".omnia-installed"
# A clipboard helper is a convenience; it must never be able to stall an install.
_CLIPBOARD_TIMEOUT_SECONDS = 10


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

    def run_stdin(self, argv: list[str], text: str) -> None:
        """Run ``argv`` feeding ``text`` on stdin (the platform clipboard tools take it there)."""


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
            raise InstallError(
                f"Command not found: {argv[0]!r}. Is it installed?"
            ) from exc
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
            raise InstallError(
                f"Command not found: {argv[0]!r}. Is it installed?"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InstallError(f"`{argv[0]} …` timed out.") from exc
        if completed.returncode != 0:
            raise InstallError(
                f"`{' '.join(argv[:3])} …` failed (exit {completed.returncode})."
            )
        return completed.stdout or ""

    def spawn(self, argv: list[str]) -> None:
        try:
            subprocess.Popen(argv)  # fixed argv, no shell
        except OSError as exc:
            raise InstallError(f"Could not launch {argv[0]!r}: {exc}") from exc

    def run_stdin(self, argv: list[str], text: str) -> None:
        try:
            subprocess.run(
                argv,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                # A ceiling, like the sibling runners have: xclip normally backgrounds itself,
                # but one that does not would hang the install worker with no limit at all.
                timeout=_CLIPBOARD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(f"{argv[0]!r} failed: {exc}") from exc


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
        # --no-install: build.py builds (and on macOS ad-hoc-signs dist/<app> with the STABLE
        # bundle-identifier requirement) but does NOT install — the installer is the single owner
        # of placement, so it knows the exact path to open and controls the copy tool (an earlier
        # bug opened a hardcoded /Applications path the build had never created → nothing launched).
        self._runner.run([venv_py, "build.py", "--no-install"], cwd=src)
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

        ``build.py --no-install`` leaves the built (and, on macOS, stable-identity-signed) app in
        ``dist/`` (buried in Anki's ``user_files``); the installer owns placing it somewhere
        launchable — and knowing that exact path to open. Tries each candidate dir in order,
        falling back if one isn't writable.
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
                self._copy_app(source, dest)
            except (
                OSError,
                shutil.Error,
                InstallError,
            ) as exc:  # not writable → next candidate
                last_err = f"{base}: {exc}"
                continue
            # macOS launches the .app bundle itself; Windows/Linux launch the inner binary.
            return dest if is_mac else dest / self._desktop_exe_name()
        raise InstallError(f"Built the app but could not install it. {last_err}")

    def _copy_app(self, source: Path, dest: Path) -> None:
        """Replace ``dest`` with a copy of the built app ``source``.

        macOS uses ``ditto`` (Apple's tool) so the stable code signature ``build.py`` applied to the
        bundle is preserved byte-for-byte — that signature is what keeps the Accessibility / Input
        Monitoring grants across rebuilds. Windows/Linux use :func:`shutil.copytree` (there is no
        ``ditto`` and no signature to preserve; ``symlinks=True`` keeps any onedir symlinks).
        """
        if self._platform == "darwin":
            self._runner.run(
                ["rm", "-rf", str(dest)]
            )  # ditto merges into an existing dir; clear it
            self._runner.run(["ditto", str(source), str(dest)])
        else:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source, dest, symlinks=True)

    def _desktop_exe_name(self) -> str:
        return (
            f"{_DESKTOP_APP_NAME}.exe"
            if self._platform.startswith("win")
            else _DESKTOP_APP_NAME
        )

    def _open_desktop_app(self, launch_path: Path) -> None:
        if self._platform == "darwin":
            self._runner.spawn(
                ["open", str(launch_path)]
            )  # open the installed .app bundle
        elif self._platform.startswith("win"):
            self._runner.spawn(["cmd", "/c", "start", "", str(launch_path)])
        else:  # linux: launch the built binary directly
            self._runner.spawn([str(launch_path)])

    # -- web: clone -> clipboard + open the finish-install page --------------------------------

    def _reveal_web(self, integration: Integration, progress: Progress) -> None:
        """Take the user as far as Chrome allows, and say where the last step is.

        Chrome has closed every route to installing an unpacked extension programmatically —
        measured on Chrome 151/152, not assumed: ``--load-extension`` is ignored, and while the
        DevTools ``Extensions.loadUnpacked`` command does load one, it is SESSION-ONLY (nothing
        is recorded in the profile, so it is gone at the next restart). A CRX outside the Web
        Store is refused. So the final click is the user's, and the job here is to leave them
        one paste away from it rather than in a file manager wondering what to do.

        It cannot even open ``chrome://extensions`` for them: Chrome drops a ``chrome://`` URL
        given on the command line and opens the new-tab page instead (see
        :mod:`~omnia.plugins.smart_notes.integration.install_page`). So what gets opened, in
        the right profile, is a local page carrying the folder, the address, and the steps.
        """
        src = self._clone_or_update(integration, progress)
        self._copy_to_clipboard(str(src))
        profile = self._chrome_profile()
        where = f" in your '{profile.name}' profile" if profile else ""
        page = self._write_install_page(integration, src, profile)
        progress(
            f"Opening the finish-install page{where}. Go to chrome://extensions, turn on "
            f"Developer mode, click 'Load unpacked', and paste the path (already copied): {src}"
        )
        self._open_chrome(page.as_uri() if page else None, profile)
        self._write_marker(src)

    def _write_install_page(
        self,
        integration: Integration,
        source: Path,
        profile: Optional[ChromeProfile],
    ) -> Optional[Path]:
        """Write the finish-install page next to the clone; None when it cannot be written.

        Beside the clone rather than inside it: the folder is about to be handed to "Load
        unpacked", and an extension directory should contain the extension and nothing else.
        """
        from omnia.plugins.smart_notes.integration.install_page import (
            render_install_page,
        )

        path = self._clones_dir / f"{integration.key}-finish-install.html"
        try:
            path.write_text(
                render_install_page(source, profile.name if profile else ""),
                encoding="utf-8",
            )
        except (
            OSError
        ):  # a page we cannot write must not fail an install that succeeded
            return None
        return path

    def _chrome_profile(self) -> Optional[ChromeProfile]:
        """The Chrome profile to target, or None when it cannot be determined."""
        from omnia.plugins.smart_notes.integration.browser import preferred_profile

        try:
            return preferred_profile(self._platform)
        except Exception:  # choosing a profile is a convenience, never a failure
            return None

    def _copy_to_clipboard(self, text: str) -> None:
        """Put ``text`` on the clipboard so 'Load unpacked' is a paste, not a hunt.

        Best-effort and silent on failure: a missing clipboard tool must not fail an install
        whose real output is the cloned folder.
        """
        try:
            if self._platform == "darwin":
                self._runner.run_stdin(["pbcopy"], text)
            elif self._platform.startswith("win"):
                self._runner.run_stdin(["clip"], text)
            else:
                self._runner.run_stdin(["xclip", "-selection", "clipboard"], text)
        except Exception:
            pass

    def _open_chrome(
        self, url: Optional[str], profile: Optional[ChromeProfile] = None
    ) -> None:
        """Open ``url`` in Chrome — in ``profile`` when one could be identified.

        Chrome numbers profile directories in creation order and shows an unrelated display
        name, so on a machine with several profiles the plain "open Chrome" shortcuts land in
        whichever one Chrome picks. ``--profile-directory`` is the only way to say which, and
        it needs Chrome's real executable: ``open -a`` / ``start chrome`` take a URL but not
        Chrome's own flags.

        ``url`` is None when the page could not be written; Chrome is still raised on the right
        profile, since the progress message carries the steps either way.
        """
        from omnia.plugins.smart_notes.integration.browser import chrome_executable

        executable = chrome_executable(self._platform) if profile is not None else None
        if executable and profile is not None:
            argv = [executable, f"--profile-directory={profile.directory}"]
            self._runner.spawn([*argv, url] if url else argv)
            return
        if url is None:  # nothing to open and no profile to aim at
            return
        if self._platform == "darwin":
            self._runner.spawn(["open", "-a", "Google Chrome", url])
        elif self._platform.startswith("win"):
            self._runner.spawn(["cmd", "/c", "start", "chrome", url])
        else:
            self._runner.spawn(["google-chrome", url])

    # -- launch an ALREADY-installed clipper (Open / Reload buttons) ---------------------------

    def launch(self, integration: Integration) -> str:
        """Open or reload an already-installed integration; return a line for the user.

        Separate from :meth:`install` because the two answer different questions. Install is a
        long job that clones, builds and copies; this is the short one a user wants when the
        thing is already there and they just want it in front of them — a desktop app that was
        quit, or an extension that needs picking up an edit.

        Args:
            integration: The registered integration to launch.

        Returns:
            A short sentence naming what happened, shown next to the button.

        Raises:
            InstallError: When it cannot be launched, with the reason a user can act on.
        """
        if integration.install_kind == "desktop":
            return self._open_installed_desktop()
        if integration.install_kind == "web":
            return self._reload_web_extension(integration)
        raise InstallError(f"{integration.name} cannot be opened from here.")

    def _open_installed_desktop(self) -> str:
        """Launch the installed desktop app, wherever this platform put it."""
        # macOS launches the .app BUNDLE; Windows and Linux launch the executable file, which
        # is exactly the split _desktop_exe_name already encodes.
        name = _DESKTOP_APP if self._platform == "darwin" else self._desktop_exe_name()
        for parent in self._app_dest_dirs():
            launch_path = parent / name
            if launch_path.exists():
                self._open_desktop_app(launch_path)
                return f"Opened {launch_path}."
        looked_in = ", ".join(str(parent) for parent in self._app_dest_dirs())
        raise InstallError(
            f"{_DESKTOP_APP_NAME} is not installed here (looked in {looked_in}). "
            "Use Install first."
        )

    def _reload_web_extension(self, integration: Integration) -> str:
        """Reload the extension in the profile Chrome last used, and show its Settings.

        Chrome gives an outside process no way to reload an unpacked extension — a ``chrome://``
        URL is dropped from the command line and DevTools ``loadUnpacked`` is session-only — but
        it does open an extension's OWN page. So this opens the clipper's options page with
        ``?omnia-reload=1``, and the extension does the rest: that page stores a flag, calls
        ``chrome.runtime.reload()``, and the fresh service worker reopens Settings.

        The id cannot be hard-coded. An unpacked extension with no manifest ``key`` gets an id
        derived from its path, so it differs per machine and has to be read out of the profile
        Chrome recorded it in.
        """
        from omnia.plugins.smart_notes.integration.browser import (
            chrome_executable,
            installed_extension_id,
        )

        if chrome_executable(self._platform) is None:
            raise InstallError(
                "Google Chrome is not installed on this machine, so there is nothing to "
                "reload. The Omnia Web Clipper is a Chrome extension."
            )
        profile = self._chrome_profile()
        if profile is None:
            raise InstallError(
                "Could not tell which Chrome profile to use. Open Chrome once, then try again."
            )
        extension_id = installed_extension_id(
            name=integration.name,
            source_dir=self._clones_dir / integration.key,
            profile=profile,
            platform=self._platform,
        )
        if extension_id is None:
            raise InstallError(
                f"{integration.name} is not loaded in Chrome profile "
                f"{profile.name!r}. Use Set up… to load it first."
            )
        url = f"chrome-extension://{extension_id}/{_WEB_OPTIONS_PAGE}?omnia-reload=1"
        self._open_chrome(url, profile)
        return f"Reloading in Chrome profile {profile.name!r}…"

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
            sha = self._runner.run_capture(
                ["git", "-C", str(src), "rev-parse", "HEAD"]
            ).strip()
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
