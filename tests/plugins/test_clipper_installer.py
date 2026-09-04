"""Tests for the one-click clipper installer orchestration (fake runner; no real git/pip/build).

Filesystem side effects (the built-app copy, the install marker) go to ``tmp_path`` via the
injected ``install_root``, so nothing here ever touches the real ``/Applications``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnia.plugins.smart_notes.integration.installer import (
    ClipperInstaller,
    InstallError,
)
from omnia.plugins.smart_notes.integration.integrations import (
    Integration,
    integration_for_key,
)

DESKTOP = Integration(
    key="desktop_clipper",
    source_tag="omnia-desktop-clipper",
    name="Omnia Desktop Clipper",
    description="",
    repo_url="https://example.test/desktop.git",
    install_kind="desktop",
)
WEB = Integration(
    key="web_clipper",
    source_tag="omnia-web-clipper",
    name="Omnia Web Clipper",
    description="",
    repo_url="https://example.test/web.git",
    install_kind="web",
)


def _raise_on_html(original):
    """Make ONLY the finish-install page unwritable — the marker write must still succeed."""

    def write_text(self, data, *args, **kwargs):
        if self.suffix == ".html":
            raise OSError("read-only")
        return original(self, data, *args, **kwargs)

    return write_text


class _FakeRunner:
    def __init__(
        self,
        fail_on: str | None = None,
        head_sha: str = "head000",
        remote_sha: str = "head000",
    ) -> None:
        self.runs: list[tuple[list[str], str | None]] = []
        self.spawns: list[list[str]] = []
        self.captures: list[tuple[list[str], str | None]] = []
        self.stdins: list[tuple[list[str], str]] = []
        self.stdin_raises = False
        self._fail_on = fail_on
        self.head_sha = head_sha
        self.remote_sha = remote_sha

    def run(self, argv, cwd=None):
        self.runs.append((argv, str(cwd) if cwd is not None else None))
        if self._fail_on and any(self._fail_on in a for a in argv):
            raise InstallError(f"boom: {self._fail_on}")
        # ``ditto`` and ``rm -rf`` are how the macOS install actually MOVES the app, so the fake
        # performs them. A recorder that only remembers the words leaves the install a no-op on
        # that platform, and any test asking "is it installed?" afterwards is answering about an
        # empty directory. (Windows/Linux copy in-process via shutil, so they need nothing here.)
        if argv[:1] == ["ditto"] and len(argv) == 3:
            source, dest = Path(argv[1]), Path(argv[2])
            if source.is_dir():
                shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        elif argv[:2] == ["rm", "-rf"] and len(argv) == 3:
            shutil.rmtree(argv[2], ignore_errors=True)

    def run_capture(self, argv, cwd=None):
        self.captures.append((argv, str(cwd) if cwd is not None else None))
        if "ls-remote" in argv:
            return f"{self.remote_sha}\trefs/heads/main\n"
        if "rev-parse" in argv:
            return f"{self.head_sha}\n"
        return ""

    def spawn(self, argv):
        self.spawns.append(argv)

    def run_stdin(self, argv, text):
        if self.stdin_raises:
            raise InstallError("no clipboard tool here")
        self.stdins.append((argv, text))


def _installer(tmp_path, runner, host_python="/usr/bin/python3", platform="darwin"):
    return ClipperInstaller(
        clones_dir=tmp_path / "clippers",
        host_python=host_python,
        runner=runner,
        platform=platform,
        install_root=tmp_path / "apps",  # never the real /Applications
    )


def _seed_build(tmp_path, *, mac: bool = True):
    """Create the dist/ artifact a successful ``build.py`` would leave, and return the clone dir."""
    clone = tmp_path / "clippers" / "desktop_clipper"
    art = "Omnia Desktop Clipper.app" if mac else "Omnia Desktop Clipper"
    (clone / "dist" / art).mkdir(parents=True)
    (clone / "dist" / art / "placeholder").write_text("x")
    if not mac:
        # PyInstaller's onedir layout: the BINARY lives inside the folder. Without it the seed
        # is a directory that only looks like an install, and a test that asks "can this be
        # opened?" gets the wrong answer for the wrong reason.
        (clone / "dist" / art / "Omnia Desktop Clipper.exe").write_text("x")
        (clone / "dist" / art / "Omnia Desktop Clipper").write_text("x")
    return clone


class TestDesktopInstall:
    def test_fresh_clone_then_venv_pip_build_install_open(self, tmp_path):
        runner = _FakeRunner()
        clone = _seed_build(tmp_path, mac=True)
        progress: list[str] = []
        _installer(tmp_path, runner).install(DESKTOP, progress.append)

        cmds = [argv for argv, _cwd in runner.runs]
        # 1) clone, 2) venv, 3) pip upgrade, 4) pip install deps, 5) build.py --no-install,
        # 6) rm old dest, 7) ditto dist->dest (macOS preserves the code signature)
        assert cmds[0][:2] == ["git", "clone"]
        assert DESKTOP.repo_url in cmds[0]
        assert cmds[1][1:3] == ["-m", "venv"]
        assert cmds[2][1:4] == ["-m", "pip", "install"] and "pip" in cmds[2][-1:]
        assert (
            cmds[3][1:5] == ["-m", "pip", "install", "-r"] and "pyinstaller" in cmds[3]
        )
        # build.py is run --no-install so the installer is the sole owner of placement
        assert cmds[4][-2:] == ["build.py", "--no-install"]
        # as_posix(): the installer was pinned to platform="darwin" so the LOGICAL path is
        # POSIX, but Path renders with backslashes when the test itself runs on Windows.
        assert Path(cmds[4][0]).as_posix().endswith("/.venv-build/bin/python")
        # macOS installs with ditto (signature-preserving) into install_root, then opens THAT path
        source = clone / "dist" / "Omnia Desktop Clipper.app"
        dest = tmp_path / "apps" / "Omnia Desktop Clipper.app"
        assert cmds[5] == ["rm", "-rf", str(dest)]
        assert cmds[6] == ["ditto", str(source), str(dest)]
        assert runner.spawns[-1] == ["open", str(dest)]
        # records the installed commit so status() can later detect an upgrade
        marker = tmp_path / "clippers" / "desktop_clipper" / ".omnia-installed"
        assert marker.read_text().strip() == runner.head_sha
        assert progress  # steps were reported

    def test_existing_checkout_pulls_instead_of_cloning(self, tmp_path):
        clone = _seed_build(tmp_path, mac=True)
        (clone / ".git").mkdir()
        runner = _FakeRunner()
        _installer(tmp_path, runner).install(DESKTOP, lambda _m: None)
        assert runner.runs[0][0][:2] == ["git", "-C"]
        assert runner.runs[0][0][3] == "pull"
        assert runner.runs[0][0][-1] == "--ff-only"

    def test_no_host_python_raises(self, tmp_path):
        runner = _FakeRunner()
        with pytest.raises(InstallError, match="No Python"):
            _installer(tmp_path, runner, host_python=None).install(
                DESKTOP, lambda _m: None
            )

    def test_build_failure_propagates(self, tmp_path):
        runner = _FakeRunner(fail_on="build.py")
        with pytest.raises(InstallError, match="boom"):
            _installer(tmp_path, runner).install(DESKTOP, lambda _m: None)

    def test_missing_build_output_raises(self, tmp_path):
        # build.py "succeeds" but leaves no app in dist/ -> a clear error, not a silent success.
        runner = _FakeRunner()
        with pytest.raises(InstallError, match="produced no app"):
            _installer(tmp_path, runner).install(DESKTOP, lambda _m: None)

    def test_windows_installs_and_launches_the_exe(self, tmp_path):
        runner = _FakeRunner()
        _seed_build(tmp_path, mac=False)
        _installer(tmp_path, runner, platform="win32").install(DESKTOP, lambda _m: None)
        # build.py is the last runner command (win/linux copy with shutil, not the runner)
        build_cmd = [argv for argv, _c in runner.runs][-1]
        assert build_cmd[-2:] == ["build.py", "--no-install"]
        assert build_cmd[0].endswith("\\Scripts\\python.exe") or build_cmd[0].endswith(
            "/Scripts/python.exe"
        )
        # installed (via shutil) to the per-user programs dir; launches the inner .exe
        assert (tmp_path / "apps" / "Omnia Desktop Clipper").is_dir()
        launch = (
            tmp_path / "apps" / "Omnia Desktop Clipper" / "Omnia Desktop Clipper.exe"
        )
        assert runner.spawns[-1] == ["cmd", "/c", "start", "", str(launch)]

    def test_linux_installs_and_launches_the_binary(self, tmp_path):
        runner = _FakeRunner()
        _seed_build(tmp_path, mac=False)
        _installer(tmp_path, runner, platform="linux").install(DESKTOP, lambda _m: None)
        assert (tmp_path / "apps" / "Omnia Desktop Clipper").is_dir()
        launch = tmp_path / "apps" / "Omnia Desktop Clipper" / "Omnia Desktop Clipper"
        assert runner.spawns[-1] == [str(launch)]


class TestWebInstall:
    """Chrome allows no programmatic install of an unpacked extension, so the last click is
    the user's. What the install owes them is landing on the right page in the right profile
    with the path already on the clipboard — not a file manager and a puzzle."""

    def _clone(self, tmp_path):
        source = tmp_path / "clippers" / "web_clipper"
        source.mkdir(parents=True, exist_ok=True)
        marker = source / ".omnia-installed"
        if marker.exists():
            marker.unlink()
        return source

    def test_it_clones_and_records_the_commit(self, tmp_path):
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert runner.runs[0][0][:2] == ["git", "clone"]  # only clones, no build
        assert len(runner.runs) == 1
        marker = tmp_path / "clippers" / "web_clipper" / ".omnia-installed"
        assert marker.read_text().strip() == runner.head_sha

    def test_the_extension_path_goes_on_the_clipboard(self, tmp_path):
        """'Load unpacked' opens a folder picker; a path on the clipboard is one paste."""
        runner = _FakeRunner()
        source = self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert runner.stdins == [(["pbcopy"], str(source))]

    @pytest.mark.parametrize(
        "platform,tool",
        [("win32", ["clip"]), ("linux", ["xclip", "-selection", "clipboard"])],
    )
    def test_windows_and_linux_use_their_own_clipboard_tool(
        self, tmp_path, platform, tool
    ):
        runner = _FakeRunner()
        source = self._clone(tmp_path)

        _installer(tmp_path, runner, platform=platform).install(WEB, lambda _m: None)

        assert runner.stdins == [(tool, str(source))]

    def test_a_clipboard_tool_that_is_missing_does_not_fail_the_install(self, tmp_path):
        """The install's real output is the cloned folder; a missing pbcopy is not a failure."""
        runner = _FakeRunner()
        runner.stdin_raises = True
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        marker = tmp_path / "clippers" / "web_clipper" / ".omnia-installed"
        assert marker.is_file()

    def test_chrome_opens_on_the_profile_chrome_itself_last_used(
        self, tmp_path, monkeypatch
    ):
        """Chrome numbers profile dirs in creation order and shows an unrelated display name,
        so on a machine with eight profiles 'just open Chrome' lands in the wrong one.
        """
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(
            browser,
            "preferred_profile",
            lambda platform="": browser.ChromeProfile("Profile 1", "phuc"),
        )
        monkeypatch.setattr(browser, "chrome_executable", lambda platform="": "/chrome")
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        argv = runner.spawns[-1]
        assert argv[:2] == ["/chrome", "--profile-directory=Profile 1"]
        assert argv[2].startswith("file://")

    def test_it_falls_back_to_plain_chrome_when_the_profile_is_unknown(
        self, tmp_path, monkeypatch
    ):
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(browser, "preferred_profile", lambda platform="": None)
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert "Google Chrome" in runner.spawns[-1]
        assert runner.spawns[-1][-1].startswith("file://")

    def test_chrome_is_never_asked_to_open_a_chrome_url(self, tmp_path, monkeypatch):
        """Chrome DROPS a ``chrome://`` URL given on the command line and opens the new-tab
        page instead — measured on Chrome 152, macOS and Windows, for every spelling of it
        including ``--app=`` and ``chrome://settings/``. Asking anyway lands the user on a
        blank tab with no idea what the install wanted from them.
        """
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(
            browser,
            "preferred_profile",
            lambda platform="": browser.ChromeProfile("Profile 1", "phuc"),
        )
        monkeypatch.setattr(browser, "chrome_executable", lambda platform="": "/chrome")
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert not any(
            arg.startswith("chrome://") for argv in runner.spawns for arg in argv
        )

    def test_the_page_it_opens_carries_the_folder_and_the_address(
        self, tmp_path, monkeypatch
    ):
        """What Chrome will not navigate to, the page has to say in words the user can paste."""
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(
            browser,
            "preferred_profile",
            lambda platform="": browser.ChromeProfile("Profile 1", "phuc"),
        )
        monkeypatch.setattr(browser, "chrome_executable", lambda platform="": "/chrome")
        runner = _FakeRunner()
        source = self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        page = tmp_path / "clippers" / "web_clipper-finish-install.html"
        assert page.is_file()
        html = page.read_text(encoding="utf-8")
        assert str(source) in html
        assert "chrome://extensions/" in html
        assert "Load unpacked" in html
        assert "phuc" in html  # names the profile it opened in
        # Written BESIDE the clone: the folder is about to be handed to "Load unpacked".
        assert page.parent == source.parent

    def test_a_page_that_cannot_be_written_still_opens_the_right_profile(
        self, tmp_path, monkeypatch
    ):
        """The install's real output is the clone; a page write is a courtesy on top of it."""
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(
            browser,
            "preferred_profile",
            lambda platform="": browser.ChromeProfile("Profile 1", "phuc"),
        )
        monkeypatch.setattr(browser, "chrome_executable", lambda platform="": "/chrome")
        monkeypatch.setattr(
            Path, "write_text", _raise_on_html(Path.write_text), raising=True
        )
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert runner.spawns[-1] == ["/chrome", "--profile-directory=Profile 1"]

    def test_the_progress_line_names_the_path_to_paste(self, tmp_path):
        """This message is what the user reads in the modal; it has to carry the next step."""
        runner = _FakeRunner()
        source = self._clone(tmp_path)
        seen: list[str] = []

        _installer(tmp_path, runner).install(WEB, seen.append)

        assert any(str(source) in message for message in seen)
        assert any("Developer mode" in message for message in seen)


class TestStatus:
    def test_not_installed_when_no_marker(self, tmp_path):
        assert _installer(tmp_path, _FakeRunner()).status(DESKTOP) == {
            "installed": False,
            "upgrade": False,
        }

    def test_up_to_date_when_marker_matches_remote(self, tmp_path):
        clone = tmp_path / "clippers" / "desktop_clipper"
        clone.mkdir(parents=True)
        (clone / ".omnia-installed").write_text("sha-1")
        st = _installer(tmp_path, _FakeRunner(remote_sha="sha-1")).status(DESKTOP)
        assert st == {"installed": True, "upgrade": False}

    def test_upgrade_when_remote_ahead(self, tmp_path):
        clone = tmp_path / "clippers" / "desktop_clipper"
        clone.mkdir(parents=True)
        (clone / ".omnia-installed").write_text("sha-old")
        st = _installer(tmp_path, _FakeRunner(remote_sha="sha-new")).status(DESKTOP)
        assert st == {"installed": True, "upgrade": True}

    def test_remote_lookup_failure_is_not_an_upgrade(self, tmp_path):
        clone = tmp_path / "clippers" / "desktop_clipper"
        clone.mkdir(parents=True)
        (clone / ".omnia-installed").write_text("sha-old")
        runner = _FakeRunner()

        def _boom(argv, cwd=None):
            raise InstallError("offline")

        runner.run_capture = _boom  # type: ignore[method-assign]
        st = _installer(tmp_path, runner).status(DESKTOP)
        assert st == {"installed": True, "upgrade": False}


class TestGuards:
    def test_non_installable_raises(self, tmp_path):
        plain = Integration(key="x", source_tag="x", name="X", description="")
        with pytest.raises(InstallError, match="can't be installed"):
            _installer(tmp_path, _FakeRunner()).install(plain, lambda _m: None)

    def test_non_installable_status_is_not_installed(self, tmp_path):
        plain = Integration(key="x", source_tag="x", name="X", description="")
        assert _installer(tmp_path, _FakeRunner()).status(plain) == {
            "installed": False,
            "upgrade": False,
        }

    def test_integration_for_key(self):
        assert integration_for_key("desktop_clipper") is not None
        assert integration_for_key("nope") is None


class TestLaunchDesktop:
    """The Integrations "Open" button: re-open the clipper that is already installed."""

    @staticmethod
    def _install_then_launch(tmp_path, platform):
        """INSTALL with the real installer, then Open — never fabricate the layout.

        The first version of these tests hand-created the file where ``_open_installed_desktop``
        happened to look, so they asserted the bug rather than the feature: PyInstaller's onedir
        layout puts the binary inside a folder of the same name, and Open was looking a level
        too high. Driving the real install is what makes them able to disagree with the code.
        """
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform=platform)
        _seed_build(tmp_path, mac=(platform == "darwin"))
        installer.install(DESKTOP, lambda _m: None)
        installed_spawn = list(runner.spawns)
        runner.spawns.clear()
        message = installer.launch(DESKTOP)
        return runner.spawns, installed_spawn, message

    def test_open_launches_exactly_what_install_launched_on_macos(self, tmp_path):
        opened, installed, message = self._install_then_launch(tmp_path, "darwin")

        assert opened == installed, "Open and Install disagree about where the app is"
        assert opened == [
            ["open", str(tmp_path / "apps" / "Omnia Desktop Clipper.app")]
        ]
        assert "Omnia Desktop Clipper.app" in message

    def test_open_launches_exactly_what_install_launched_on_windows(self, tmp_path):
        """The onedir folder is the level that was missing; the exe lives INSIDE it."""
        opened, installed, _ = self._install_then_launch(tmp_path, "win32")

        expected = (
            tmp_path / "apps" / "Omnia Desktop Clipper" / "Omnia Desktop Clipper.exe"
        )
        assert opened == installed
        assert opened == [["cmd", "/c", "start", "", str(expected)]]

    def test_open_launches_exactly_what_install_launched_on_linux(self, tmp_path):
        """Worse than Windows if this drifts: exists() is True for the folder, so the check
        passes and the spawn tries to execute a DIRECTORY."""
        opened, installed, _ = self._install_then_launch(tmp_path, "linux")

        expected = tmp_path / "apps" / "Omnia Desktop Clipper" / "Omnia Desktop Clipper"
        assert opened == installed
        assert opened == [[str(expected)]]
        assert expected.is_file(), "Open would be spawning a directory"

    def test_nothing_installed_says_so_and_launches_nothing(self, tmp_path):
        """The failure a user can act on: it names where it looked and what to do."""
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        with pytest.raises(InstallError) as excinfo:
            installer.launch(DESKTOP)

        assert "not installed" in str(excinfo.value)
        assert "Install" in str(excinfo.value)
        assert runner.spawns == []


class TestLaunchWeb:
    """The Integrations "Reload" button: reload the extension and show its Settings."""

    @staticmethod
    def _patch_browser(
        monkeypatch,
        *,
        chrome="/chrome",
        profile=...,
        extension_id="abc123",
        profiles=...,
        found_in=None,
    ):
        """Stub every browser seam Reload touches, so no test reads this machine's Chrome.

        Reload now searches ALL profiles via ``chrome_profiles``; leaving that unstubbed made
        the suite read the developer's real ``Local State`` and pass or fail depending on
        which Chrome profiles happen to exist on the machine running it.

        ``profiles`` is the search order (default: just ``profile``). ``found_in`` names the
        one directory that has the extension; None means every profile reports it, so the
        first in order wins, and ``extension_id=None`` means none does.
        """
        from omnia.plugins.smart_notes.integration import browser

        if profile is ...:
            profile = browser.ChromeProfile(directory="Profile 1", name="phuc")
        if profiles is ...:
            profiles = [profile] if profile is not None else []
        monkeypatch.setattr(browser, "chrome_executable", lambda *a, **k: chrome)
        monkeypatch.setattr(browser, "preferred_profile", lambda *a, **k: profile)
        monkeypatch.setattr(browser, "chrome_profiles", lambda *a, **k: list(profiles))

        def _installed(*_args, profile=None, **_kwargs):
            if extension_id is None:
                return None
            if found_in is not None and (
                profile is None or profile.directory != found_in
            ):
                return None
            return extension_id

        monkeypatch.setattr(browser, "installed_extension_id", _installed)
        return profile

    def test_it_opens_the_extension_page_with_the_reload_flag(
        self, tmp_path, monkeypatch
    ):
        """The whole mechanism in one assertion: our own page, in the right profile.

        Chrome refuses every other route — a ``chrome://`` URL is dropped from the command line
        and DevTools loadUnpacked is session-only — so the extension's own page carrying
        ``?omnia-reload=1`` IS the feature.
        """
        self._patch_browser(monkeypatch)
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        message = installer.launch(WEB)

        assert len(runner.spawns) == 1
        argv = runner.spawns[0]
        assert argv[0] == "/chrome"
        assert "--profile-directory=Profile 1" in argv
        assert argv[-1] == "chrome-extension://abc123/src/options.html?omnia-reload=1"
        assert "phuc" in message

    def test_chrome_is_resolved_once_per_click(self, tmp_path, monkeypatch):
        """One filesystem sweep, not two. Reload is meant to feel instant."""
        from omnia.plugins.smart_notes.integration import browser

        sweeps = {"n": 0}

        def counting(*_args, **_kwargs):
            sweeps["n"] += 1
            return "/chrome"

        self._patch_browser(monkeypatch)
        monkeypatch.setattr(browser, "chrome_executable", counting)
        installer = _installer(tmp_path, _FakeRunner(), platform="darwin")

        installer.launch(WEB)

        assert sweeps["n"] == 1

    def test_no_chrome_is_an_error_not_a_silent_no_op(self, tmp_path, monkeypatch):
        """Explicitly required: a machine without Chrome must SAY so."""
        self._patch_browser(monkeypatch, chrome=None)
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="win32")

        with pytest.raises(InstallError) as excinfo:
            installer.launch(WEB)

        assert "Chrome" in str(excinfo.value)
        assert runner.spawns == []

    def test_no_profile_is_an_error(self, tmp_path, monkeypatch):
        """Chrome installed but never opened: there is nothing to search, and Reload says so."""
        self._patch_browser(monkeypatch, profile=None, profiles=[])
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        with pytest.raises(InstallError):
            installer.launch(WEB)

        assert runner.spawns == []

    def test_it_finds_the_extension_in_a_profile_that_is_not_the_preferred_one(
        self, tmp_path, monkeypatch
    ):
        """The reported bug, end to end.

        Chrome last used "Default"; the clipper is loaded and running in "Profile 3". Reload
        used to look only in "Default" and report a running extension as absent. It must now
        find it AND open Chrome on the profile that has it — the id is only valid there.
        """
        from omnia.plugins.smart_notes.integration import browser

        default = browser.ChromeProfile(directory="Default", name="moreh.com.vn")
        third = browser.ChromeProfile(directory="Profile 3", name="phuc")
        self._patch_browser(
            monkeypatch,
            profile=default,
            profiles=[default, third],
            extension_id="jmdh",
            found_in="Profile 3",
        )
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        message = installer.launch(WEB)

        assert len(runner.spawns) == 1
        argv = runner.spawns[0]
        assert "--profile-directory=Profile 3" in argv
        assert "--profile-directory=Default" not in argv
        assert argv[-1] == "chrome-extension://jmdh/src/options.html?omnia-reload=1"
        assert "phuc" in message

    def test_the_preferred_profile_still_wins_when_it_has_the_extension(
        self, tmp_path, monkeypatch
    ):
        """Widening the search must not change the case that already worked."""
        from omnia.plugins.smart_notes.integration import browser

        default = browser.ChromeProfile(directory="Default", name="moreh.com.vn")
        third = browser.ChromeProfile(directory="Profile 3", name="phuc")
        self._patch_browser(
            monkeypatch,
            profile=default,
            profiles=[default, third],
            extension_id="abc123",
        )
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        installer.launch(WEB)

        assert "--profile-directory=Default" in runner.spawns[0]

    def test_an_extension_that_is_not_loaded_says_where_to_load_it(
        self, tmp_path, monkeypatch
    ):
        """Nothing loaded anywhere: the message must name things that EXIST on screen.

        The old text said "Use Set up…". That button only renders when the clipper is NOT
        installed; once installed and current it reads "Up to date" and is disabled, so the
        user was pointed at a control they could not click. What genuinely exists in that
        state is Chrome's own extensions page and the cloned folder — so those are named,
        along with which profiles were searched.
        """
        from omnia.plugins.smart_notes.integration import browser

        default = browser.ChromeProfile(directory="Default", name="moreh.com.vn")
        third = browser.ChromeProfile(directory="Profile 3", name="phuc")
        self._patch_browser(
            monkeypatch, profile=default, profiles=[default, third], extension_id=None
        )
        runner = _FakeRunner()
        installer = _installer(tmp_path, runner, platform="darwin")

        with pytest.raises(InstallError) as excinfo:
            installer.launch(WEB)

        text = str(excinfo.value)
        assert (
            "Set up" not in text
        ), "names a button that is not on screen in this state"
        assert "chrome://extensions" in text
        assert "Load unpacked" in text
        assert str(tmp_path / "clippers" / "web_clipper") in text
        assert (
            "moreh.com.vn" in text and "phuc" in text
        ), "says which profiles were searched"
        assert runner.spawns == []

    def test_the_not_loaded_message_offers_the_finish_page_when_one_exists(
        self, tmp_path, monkeypatch
    ):
        """An earlier install wrote a finish-install page; the message should hand it back."""
        self._patch_browser(monkeypatch, extension_id=None)
        installer = _installer(tmp_path, _FakeRunner(), platform="darwin")
        page = tmp_path / "clippers" / "web_clipper-finish-install.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html></html>", encoding="utf-8")

        with pytest.raises(InstallError) as excinfo:
            installer.launch(WEB)

        assert str(page) in str(excinfo.value)

    def test_it_looks_the_extension_up_in_the_clone_it_installed(
        self, tmp_path, monkeypatch
    ):
        """Matching on OUR clone, not just the name, so another build of it is not reloaded."""
        from omnia.plugins.smart_notes.integration import browser

        seen: dict = {}
        self._patch_browser(monkeypatch)
        monkeypatch.setattr(
            browser,
            "installed_extension_id",
            lambda **kwargs: seen.update(kwargs) or "abc123",
        )
        installer = _installer(tmp_path, _FakeRunner(), platform="darwin")

        installer.launch(WEB)

        assert seen["source_dir"] == tmp_path / "clippers" / "web_clipper"
        assert seen["name"] == WEB.name


class TestLaunchDispatch:
    def test_an_integration_with_no_install_kind_cannot_be_launched(self, tmp_path):
        from omnia.plugins.smart_notes.integration.integrations import Integration

        installer = _installer(tmp_path, _FakeRunner())
        plain = Integration(key="x", source_tag="x", name="X", description="")

        with pytest.raises(InstallError):
            installer.launch(plain)
