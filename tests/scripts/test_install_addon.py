"""Tests for ``scripts/install_addon.py``: where it installs, and how it links.

Most of the script places files into a real Anki install, which is not something the suite
should be performing. Two parts are still worth pinning:

* **Where** it installs is pure logic, and getting it wrong is silent — the script reports
  success while the add-on lands in a folder the running Anki never reads, which looks exactly
  like "my code change did nothing".
* **How** each item is placed and, above all, **removed**, because that is where the script's
  own idempotence lives and no two platforms agree about it: POSIX ``rmdir`` refuses a symlink,
  Windows ``unlink`` refuses a directory symlink, ``shutil.rmtree`` refuses a reparse point,
  and ``is_dir()`` — the obvious thing to branch on — answers a question about the TARGET. A
  wrong choice here leaves an install that works exactly once and fails on every re-run, on
  whichever platform the author did not happen to be using.
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

        note = install_addon._place(source, tmp_path / "placed", copy=False)

        assert junctions == [(source, tmp_path / "placed")]
        assert "junction" in note

    def test_a_file_is_copied_because_a_junction_cannot_cover_one(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(install_addon.sys, "platform", "win32")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
        source = tmp_path / "envs.py"
        source.write_text("KNOB = 1\n", encoding="utf-8")

        note = install_addon._place(source, tmp_path / "placed.py", copy=False)

        assert (tmp_path / "placed.py").read_text(encoding="utf-8") == "KNOB = 1\n"
        assert "copied" in note

    def test_a_symlink_that_works_reports_no_fallback(self, tmp_path):
        """The note is what the run prints; a normal install must have nothing to report."""
        source = tmp_path / "core"
        source.mkdir()
        placed = []
        # Substituting the call keeps this assertion about the RETURN CONTRACT rather than
        # about whether this particular machine can make a symlink.
        original = Path.symlink_to

        def _record(self, target, target_is_directory=False):
            placed.append((self, target))

        Path.symlink_to = _record
        try:
            note = install_addon._place(source, tmp_path / "placed", copy=False)
        finally:
            Path.symlink_to = original

        assert note is None
        assert placed == [(tmp_path / "placed", source)]

    def test_a_destination_that_already_exists_is_not_a_missing_privilege(
        self, tmp_path, monkeypatch
    ):
        """FileExistsError means the caller failed to clear a prior assembly. Treating it as
        "the OS refused a symlink" would hide that behind a junction attempt whose real error
        is a CalledProcessError from cmd."""
        monkeypatch.setattr(install_addon.sys, "platform", "win32")
        monkeypatch.setattr(
            install_addon,
            "_create_junction",
            lambda s, d: pytest.fail(
                "must not attempt a junction for an existing dest"
            ),
        )
        source = tmp_path / "core"
        source.mkdir()
        (tmp_path / "placed").mkdir()

        def _exists(*_args, **_kwargs):
            raise FileExistsError(17, "File exists")

        monkeypatch.setattr(Path, "symlink_to", _exists)

        with pytest.raises(FileExistsError):
            install_addon._place(source, tmp_path / "placed", copy=False)

    def test_elsewhere_the_error_still_propagates(self, tmp_path, monkeypatch):
        """Only Windows lacks the privilege; anywhere else a refused symlink is a real fault
        and must not be papered over with a silent copy."""
        monkeypatch.setattr(install_addon.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
        source = tmp_path / "core"
        source.mkdir()

        with pytest.raises(OSError):
            install_addon._place(source, tmp_path / "placed", copy=False)


class TestRemoveLinkTriesRatherThanPredicts:
    """No property of the path answers "is this a directory link?" reliably: ``is_dir()``
    FOLLOWS the link (so a POSIX symlink-to-a-directory looks like a directory, and ``rmdir``
    refuses it with ENOTDIR) and a DANGLING Windows directory symlink looks like a file (so
    ``unlink`` refuses it). Attempting ``rmdir`` and falling back is the only shape that covers
    all four. Monkeypatched, so the order is pinned on any machine — including one that cannot
    create a symlink at all.
    """

    def _spy(self, monkeypatch, *, rmdir_raises):
        calls = []

        def _rmdir(path):
            calls.append(("rmdir", path))
            if rmdir_raises is not None:
                raise rmdir_raises

        monkeypatch.setattr(install_addon.os, "rmdir", _rmdir)
        monkeypatch.setattr(
            Path, "unlink", lambda _self, **_kw: calls.append(("unlink", _self))
        )
        return calls

    def test_a_directory_link_is_removed_by_rmdir_alone(self, monkeypatch):
        """A Windows junction or a directory symlink, live or dangling."""
        calls = self._spy(monkeypatch, rmdir_raises=None)

        install_addon._remove_link(Path("addon/core"))

        assert calls == [("rmdir", Path("addon/core"))]

    def test_a_posix_symlink_falls_back_to_unlink(self, monkeypatch):
        """The regression this class exists for: POSIX rmdir refuses a symlink outright, and
        giving up there aborted every re-install on macOS and Linux."""
        calls = self._spy(
            monkeypatch, rmdir_raises=NotADirectoryError(20, "Not a directory")
        )

        install_addon._remove_link(Path("addon/core"))

        assert calls == [("rmdir", Path("addon/core")), ("unlink", Path("addon/core"))]

    def test_a_file_link_falls_back_to_unlink(self, monkeypatch):
        calls = self._spy(
            monkeypatch, rmdir_raises=OSError(267, "The directory name is invalid")
        )

        install_addon._remove_link(Path("addon/envs.py"))

        assert calls == [
            ("rmdir", Path("addon/envs.py")),
            ("unlink", Path("addon/envs.py")),
        ]

    def test_rmdir_is_always_attempted_first(self, monkeypatch):
        """Order matters: unlink-first would delete a Windows junction's ENTRY on some paths
        and refuse on others, and the fallback would never be reached for the dangling case.
        """
        calls = self._spy(monkeypatch, rmdir_raises=OSError(1, "nope"))

        install_addon._remove_link(Path("addon/anything"))

        assert calls[0][0] == "rmdir"


class TestAJunctionThatCannotBeMade:
    def test_mklinks_reason_reaches_the_user(self, monkeypatch, tmp_path):
        """``capture_output=True`` swallows the one line that says why — and junctions really
        are unsupported in places (a UNC path, a non-NTFS volume). Without this the install
        ends as a bare 'returned non-zero exit status 1'."""
        # ``sys.modules[name] = None`` makes ``import name`` raise ImportError, which is how
        # the shell fallback becomes the path under test on a machine that HAS _winapi.
        monkeypatch.setitem(sys.modules, "_winapi", None)

        def _fail(*_args, **_kwargs):
            raise install_addon.subprocess.CalledProcessError(
                1,
                "mklink",
                output="",
                stderr="Local volumes are required to complete the operation.\n",
            )

        monkeypatch.setattr(install_addon.subprocess, "run", _fail)

        with pytest.raises(SystemExit) as excinfo:
            install_addon._create_junction(tmp_path / "src", tmp_path / "dest")

        message = str(excinfo.value)
        assert "Local volumes are required" in message
        assert "--copy" in message


@pytest.fixture
def real_symlinks(tmp_path):
    """Skip where the OS will not make a symlink (Windows without Developer Mode).

    The monkeypatched class above pins the branch everywhere; these tests prove the real call
    against the real filesystem, on the platforms that can.
    """
    probe = tmp_path / "_probe_target"
    probe.mkdir()
    try:
        (tmp_path / "_probe_link").symlink_to(probe, target_is_directory=True)
    except OSError:
        pytest.skip(
            "this machine cannot create symlinks (Windows without Developer Mode)"
        )
    return True


@pytest.mark.usefixtures("real_symlinks")
class TestClearingAPriorSymlinkAssembly:
    """The re-run path against real symlinks: clearing must drop the LINK and leave the repo's
    own files where they are."""

    def test_a_symlinked_item_is_removed_and_its_target_survives(self, tmp_path):
        source = tmp_path / "repo" / "core"
        source.mkdir(parents=True)
        (source / "registry.py").write_text("KEEP ME\n", encoding="utf-8")
        target = tmp_path / "addon"
        target.mkdir()
        (target / "core").symlink_to(source, target_is_directory=True)

        install_addon._clear_prior_assembly(target)

        assert not (target / "core").exists()
        assert (source / "registry.py").read_text(encoding="utf-8") == "KEEP ME\n"

    def test_a_symlinked_FILE_is_removed_and_its_target_survives(self, tmp_path):
        source = tmp_path / "repo" / "envs.py"
        source.parent.mkdir(parents=True)
        source.write_text("KNOB = 1\n", encoding="utf-8")
        target = tmp_path / "addon"
        target.mkdir()
        (target / "envs.py").symlink_to(source)

        install_addon._clear_prior_assembly(target)

        assert not (target / "envs.py").exists()
        assert source.read_text(encoding="utf-8") == "KNOB = 1\n"

    def test_a_whole_folder_symlink_is_unlinked(self, tmp_path):
        """The old ``install_dev`` layout: the add-on folder ITSELF was one symlink. Same
        helper, same trap, and this is the upgrade path onto the per-item assembly."""
        source = tmp_path / "repo" / "omnia"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        target = tmp_path / "addons21" / "omnia"
        target.parent.mkdir(parents=True)
        target.symlink_to(source, target_is_directory=True)

        install_addon._clear_prior_assembly(target)

        assert not target.exists()
        assert (source / "__init__.py").read_text(encoding="utf-8") == "x = 1\n"


class TestClearingKeepsWhatTheUserOwns:
    def test_the_runtime_dirs_are_left_alone(self, tmp_path):
        """``user_files`` holds the user's config, secrets and downloaded voices; a re-run
        clears only the source/sibling links."""
        target = tmp_path / "addon"
        (target / "user_files" / "config").mkdir(parents=True)
        (target / "user_files" / "config" / "providers.toml").write_text(
            "[llm]\n", encoding="utf-8"
        )

        install_addon._clear_prior_assembly(target)

        assert (target / "user_files" / "config" / "providers.toml").read_text(
            encoding="utf-8"
        ) == "[llm]\n"

    def test_a_prior_copy_install_is_replaced(self, tmp_path):
        """A ``--copy`` run leaves REAL directories where links normally go; those must be
        cleared, or the next run's symlink hits FileExistsError."""
        target = tmp_path / "addon"
        (target / "core").mkdir(parents=True)
        (target / "core" / "registry.py").write_text("stale\n", encoding="utf-8")

        install_addon._clear_prior_assembly(target)

        assert not (target / "core").exists()


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
