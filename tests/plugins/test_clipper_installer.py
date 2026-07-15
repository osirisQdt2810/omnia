"""Tests for the one-click clipper installer orchestration (fake runner; no real git/pip/build)."""

from __future__ import annotations

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
    def __init__(self, fail_on: str | None = None) -> None:
        self.runs: list[tuple[list[str], str | None]] = []
        self.spawns: list[list[str]] = []
        self._fail_on = fail_on

    def run(self, argv, cwd=None):
        self.runs.append((argv, str(cwd) if cwd is not None else None))
        if self._fail_on and any(self._fail_on in a for a in argv):
            raise InstallError(f"boom: {self._fail_on}")

    def spawn(self, argv):
        self.spawns.append(argv)


def _installer(tmp_path, runner, host_python="/usr/bin/python3", platform="darwin"):
    return ClipperInstaller(
        clones_dir=tmp_path / "clippers",
        host_python=host_python,
        runner=runner,
        platform=platform,
    )


class TestDesktopInstall:
    def test_fresh_clone_then_venv_pip_build_open(self, tmp_path):
        runner = _FakeRunner()
        progress: list[str] = []
        _installer(tmp_path, runner).install(DESKTOP, progress.append)

        cmds = [argv for argv, _cwd in runner.runs]
        # 1) clone (no .git yet), 2) venv, 3) pip upgrade, 4) pip install deps, 5) build.py
        assert cmds[0][:2] == ["git", "clone"]
        assert DESKTOP.repo_url in cmds[0]
        assert cmds[1][1:3] == ["-m", "venv"]
        assert cmds[2][1:4] == ["-m", "pip", "install"] and "pip" in cmds[2][-1:]
        assert cmds[3][1:5] == ["-m", "pip", "install", "-r"] and "pyinstaller" in cmds[3]
        assert cmds[4][-1] == "build.py"
        # venv python used for pip/build is the macOS bin/python of the clone's .venv-build
        assert cmds[4][0].endswith("/.venv-build/bin/python")
        # opens the installed app
        assert runner.spawns and runner.spawns[-1][:2] == ["open", "-a"]
        assert "/Applications/Omnia Desktop Clipper.app" in runner.spawns[-1]
        assert progress  # steps were reported

    def test_existing_checkout_pulls_instead_of_cloning(self, tmp_path):
        (tmp_path / "clippers" / "desktop_clipper" / ".git").mkdir(parents=True)
        runner = _FakeRunner()
        _installer(tmp_path, runner).install(DESKTOP, lambda _m: None)
        assert runner.runs[0][0][:2] == ["git", "-C"]
        assert runner.runs[0][0][3] == "pull"
        assert runner.runs[0][0][-1] == "--ff-only"

    def test_no_host_python_raises(self, tmp_path):
        runner = _FakeRunner()
        with pytest.raises(InstallError, match="No Python"):
            _installer(tmp_path, runner, host_python=None).install(DESKTOP, lambda _m: None)

    def test_build_failure_propagates(self, tmp_path):
        runner = _FakeRunner(fail_on="build.py")
        with pytest.raises(InstallError, match="boom"):
            _installer(tmp_path, runner).install(DESKTOP, lambda _m: None)

    def test_windows_uses_scripts_python(self, tmp_path):
        runner = _FakeRunner()
        _installer(tmp_path, runner, platform="win32").install(DESKTOP, lambda _m: None)
        build_cmd = [argv for argv, _c in runner.runs][-1]
        assert build_cmd[0].endswith("\\Scripts\\python.exe") or build_cmd[0].endswith(
            "/Scripts/python.exe"
        )


class TestWebReveal:
    def test_clone_then_reveal_and_open_chrome(self, tmp_path):
        runner = _FakeRunner()
        _installer(tmp_path, runner).install(WEB, lambda _m: None)
        assert runner.runs[0][0][:2] == ["git", "clone"]  # only clones, no build
        assert len(runner.runs) == 1
        assert runner.spawns[0][:2] == ["open", "-R"]  # reveal folder
        assert "Google Chrome" in runner.spawns[1] and "chrome://extensions/" in runner.spawns[1]


class TestGuards:
    def test_non_installable_raises(self, tmp_path):
        plain = Integration(key="x", source_tag="x", name="X", description="")
        with pytest.raises(InstallError, match="can't be installed"):
            _installer(tmp_path, _FakeRunner()).install(plain, lambda _m: None)

    def test_integration_for_key(self):
        assert integration_for_key("desktop_clipper") is not None
        assert integration_for_key("nope") is None
