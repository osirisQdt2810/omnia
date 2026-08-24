"""Tests for the one-click clipper installer orchestration (fake runner; no real git/pip/build).

Filesystem side effects (the built-app copy, the install marker) go to ``tmp_path`` via the
injected ``install_root``, so nothing here ever touches the real ``/Applications``.
"""

from __future__ import annotations

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

        assert runner.spawns[-1] == [
            "/chrome",
            "--profile-directory=Profile 1",
            "chrome://extensions/",
        ]

    def test_it_falls_back_to_plain_chrome_when_the_profile_is_unknown(
        self, tmp_path, monkeypatch
    ):
        from omnia.plugins.smart_notes.integration import browser

        monkeypatch.setattr(browser, "preferred_profile", lambda platform="": None)
        runner = _FakeRunner()
        self._clone(tmp_path)

        _installer(tmp_path, runner).install(WEB, lambda _m: None)

        assert "chrome://extensions/" in runner.spawns[-1]
        assert "Google Chrome" in runner.spawns[-1]

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
