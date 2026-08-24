"""Tests for ``scripts/install_addon.py``: where it installs, and how it links.

Most of the script places files into a real Anki install, which is not something the suite
should be performing. Two parts are still worth pinning:

* **Where** it installs is pure logic, and getting it wrong is silent — the script reports
  success while the add-on lands in a folder the running Anki never reads, which looks exactly
  like "my code change did nothing".
* **How** each item is placed, because the Windows fallback path breaks the script's own
  idempotence if it is wrong: a junction mistaken for a real directory is handed to
  ``shutil.rmtree``, which refuses it outright, so the install works once and fails on every
  re-run.
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


@pytest.fixture(autouse=True)
def _clean_fallback_log():
    """``_FALLBACKS`` is module state the run prints at the end; don't leak it between tests."""
    install_addon._FALLBACKS.clear()
    yield
    install_addon._FALLBACKS.clear()


def _refuse_symlink(*_args, **_kwargs):
    """Stand in for Windows without Developer Mode, which is where the fallback matters."""
    raise OSError(1314, "A required privilege is not held by the client")


class TestPlacingAnItemWhenTheOsRefusesASymlink:
    """Windows needs Developer Mode or an elevated shell to make a symlink. Without one the
    install used to abort outright, so a stock Windows box could not follow the README.
    """

    def test_a_directory_becomes_a_junction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install_addon.sys, "platform", "win32")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
        junctions = []
        monkeypatch.setattr(
            install_addon, "_create_junction", lambda s, d: junctions.append((s, d))
        )
        source = tmp_path / "core"
        source.mkdir()

        install_addon._place(source, tmp_path / "placed", copy=False)

        assert junctions == [(source, tmp_path / "placed")]
        assert any("junction" in note for note in install_addon._FALLBACKS)

    def test_a_file_is_copied_because_a_junction_cannot_cover_one(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(install_addon.sys, "platform", "win32")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
        source = tmp_path / "envs.py"
        source.write_text("KNOB = 1\n", encoding="utf-8")

        install_addon._place(source, tmp_path / "placed.py", copy=False)

        assert (tmp_path / "placed.py").read_text(encoding="utf-8") == "KNOB = 1\n"
        assert any("copied" in note for note in install_addon._FALLBACKS)

    def test_elsewhere_the_error_still_propagates(self, tmp_path, monkeypatch):
        """Only Windows lacks the privilege; anywhere else a refused symlink is a real fault
        and must not be papered over with a silent copy."""
        monkeypatch.setattr(install_addon.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
        source = tmp_path / "core"
        source.mkdir()

        with pytest.raises(OSError):
            install_addon._place(source, tmp_path / "placed", copy=False)


@pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="junctions exist only on Windows"
)
class TestAJunctionCountsAsALink:
    def test_is_link_recognises_one(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        link = tmp_path / "link"
        install_addon._create_junction(source, link)

        assert install_addon._is_link(link) is True
        assert install_addon._is_link(source) is False

    def test_clearing_a_prior_assembly_does_not_delete_through_it(self, tmp_path):
        """``_clear_prior_assembly`` must remove the LINK and leave the target alone. Read as
        a plain directory instead, a junction goes to ``shutil.rmtree``, which refuses it and
        aborts the whole re-run."""
        source = tmp_path / "repo" / "core"
        source.mkdir(parents=True)
        (source / "registry.py").write_text("KEEP ME\n", encoding="utf-8")
        target = tmp_path / "addon"
        target.mkdir()
        install_addon._create_junction(source, target / "core")

        install_addon._clear_prior_assembly(target)

        assert not (target / "core").exists()
        assert (source / "registry.py").read_text(encoding="utf-8") == "KEEP ME\n"
