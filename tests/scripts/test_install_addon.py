"""Tests for ``scripts/install_addon.py``'s notion of where Anki lives.

Only the base-folder resolution is covered: the rest of the script symlinks into a real Anki
install and is not something the suite should be performing. But *where* it installs is pure
logic, and getting it wrong is silent — the script reports success while the add-on lands in a
folder the running Anki never reads, which looks exactly like "my code change did nothing".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import install_addon  # noqa: E402


class TestAnkiBaseDir:
    def test_anki_base_env_var_wins_on_every_platform(self, monkeypatch):
        """``ANKI_BASE`` is how Anki itself is pointed at a throwaway profile.

        If the script ignored it, running a second Anki for testing would load the add-on from
        the DEFAULT base — so you would be testing the previously installed copy while believing
        you were testing the new one.
        """
        monkeypatch.setenv("ANKI_BASE", "/tmp/some-test-base")

        for platform in ("darwin", "win32", "linux"):
            monkeypatch.setattr(install_addon.sys, "platform", platform)

            assert install_addon.anki_base_dir() == Path(
                "/tmp/some-test-base"
            ), platform

    def test_an_empty_anki_base_is_ignored(self, monkeypatch):
        # An exported-but-empty variable must not resolve the base to the current directory.
        monkeypatch.setenv("ANKI_BASE", "")
        monkeypatch.setattr(install_addon.sys, "platform", "darwin")

        assert install_addon.anki_base_dir() != Path("")
        assert install_addon.anki_base_dir().name == "Anki2"

    def test_macos_default(self, monkeypatch):
        monkeypatch.delenv("ANKI_BASE", raising=False)
        monkeypatch.setattr(install_addon.sys, "platform", "darwin")

        assert install_addon.anki_base_dir() == (
            Path.home() / "Library" / "Application Support" / "Anki2"
        )

    def test_windows_uses_appdata(self, monkeypatch):
        monkeypatch.delenv("ANKI_BASE", raising=False)
        monkeypatch.setattr(install_addon.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\Someone\AppData\Roaming")

        assert (
            install_addon.anki_base_dir()
            == Path(r"C:\Users\Someone\AppData\Roaming") / "Anki2"
        )

    def test_windows_without_appdata_falls_back_home(self, monkeypatch):
        monkeypatch.delenv("ANKI_BASE", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(install_addon.sys, "platform", "win32")

        assert install_addon.anki_base_dir() == Path.home() / "Anki2"

    def test_linux_default(self, monkeypatch):
        monkeypatch.delenv("ANKI_BASE", raising=False)
        monkeypatch.setattr(install_addon.sys, "platform", "linux")

        assert install_addon.anki_base_dir() == (
            Path.home() / ".local" / "share" / "Anki2"
        )

    @pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
    def test_addons_dir_is_always_addons21_under_the_base(self, monkeypatch, platform):
        monkeypatch.setenv("ANKI_BASE", "/tmp/base")
        monkeypatch.setattr(install_addon.sys, "platform", platform)

        assert install_addon.anki_addons_dir() == Path("/tmp/base/addons21")
