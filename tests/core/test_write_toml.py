"""A config write is all or nothing: the file on disk is the old one or the new one, never half.

`write_toml` truncated the file and then serialised into it. A crash, a full disk or a value
tomli_w refuses therefore left a truncated providers.toml, and the next start raised
TOMLDecodeError before any of the add-on loaded — with the user's keys and endpoints in a file
Omnia could no longer read.
"""

from __future__ import annotations

import os
import stat

import pytest

from omnia.core.config import loader
from omnia.core.config.loader import read_toml, write_toml

_BEFORE = '[llm]\nprovider = "gemini"\n'


@pytest.fixture
def existing(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text(_BEFORE, encoding="utf-8")
    return path


class TestAWriteIsAllOrNothing:
    def test_it_writes_what_it_was_given(self, existing):
        write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert read_toml(existing) == {"llm": {"provider": "custom:gpu"}}

    def test_a_crash_before_the_file_is_replaced_leaves_the_old_one(
        self, existing, monkeypatch
    ):
        def power_loss(*_args):
            raise OSError("power loss")

        monkeypatch.setattr(loader.os, "replace", power_loss)
        with pytest.raises(OSError):
            write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert existing.read_text(encoding="utf-8") == _BEFORE

    def test_a_failed_write_leaves_nothing_half_written_behind(
        self, existing, monkeypatch
    ):
        def power_loss(*_args):
            raise OSError("power loss")

        monkeypatch.setattr(loader.os, "replace", power_loss)
        with pytest.raises(OSError):
            write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert sorted(p.name for p in existing.parent.iterdir()) == ["providers.toml"]

    def test_a_value_tomli_w_refuses_leaves_the_old_file(self, existing):
        with pytest.raises(TypeError):
            write_toml(existing, {"llm": {"provider": object()}})

        assert existing.read_text(encoding="utf-8") == _BEFORE
        assert sorted(p.name for p in existing.parent.iterdir()) == ["providers.toml"]

    def test_the_new_file_is_written_beside_the_old_one(self, existing, monkeypatch):
        """`os.replace` is atomic only within one filesystem, which a temp dir elsewhere
        need not share."""
        moves = []
        replace = os.replace

        def watching(src, dst):
            moves.append((os.path.dirname(src), os.path.dirname(dst)))
            replace(src, dst)

        monkeypatch.setattr(loader.os, "replace", watching)
        write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert moves == [(str(existing.parent), str(existing.parent))]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_files_permissions_are_kept(self, existing):
        """A file the user narrowed must not be widened by a rewrite, nor the reverse."""
        existing.chmod(0o640)

        write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert stat.S_IMODE(existing.stat().st_mode) == 0o640

    def test_a_new_file_is_created_with_its_parents(self, tmp_path):
        path = tmp_path / "config" / "providers.toml"

        write_toml(path, {"llm": {"provider": "gemini"}})

        assert read_toml(path) == {"llm": {"provider": "gemini"}}


class TestWhereTheWriteLands:
    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_link_the_user_made_keeps_pointing_where_it_did(self, tmp_path):
        """Replaced by path, a symlinked providers.toml became a plain file and the file it
        named went stale; the file it names is the one written."""
        real = tmp_path / "elsewhere.toml"
        real.write_text(_BEFORE, encoding="utf-8")
        link = tmp_path / "providers.toml"
        link.symlink_to(real)

        write_toml(link, {"llm": {"provider": "custom:gpu"}})

        assert link.is_symlink()
        assert read_toml(real) == {"llm": {"provider": "custom:gpu"}}


class TestAWindowsSharingRefusal:
    """On Windows an antivirus scan or the indexer holding a file for a moment refuses the
    replace with PermissionError, where an in-place write would have gone through."""

    def _flaky_replace(self, monkeypatch, refusals):
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(target)
            if len(calls) <= refusals:
                raise PermissionError(32, "The process cannot access the file")
            real(source, target)

        monkeypatch.setattr(loader.os, "replace", replace)
        monkeypatch.setattr(loader.time, "sleep", lambda _s: None)
        return calls

    def test_it_is_retried_where_it_happens(self, existing, monkeypatch):
        calls = self._flaky_replace(monkeypatch, refusals=2)
        monkeypatch.setattr(loader, "_REPLACE_ATTEMPTS", 5)

        write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert len(calls) == 3
        assert read_toml(existing) == {"llm": {"provider": "custom:gpu"}}

    def test_a_refusal_that_lasts_leaves_the_old_file_and_no_temp(
        self, existing, monkeypatch
    ):
        self._flaky_replace(monkeypatch, refusals=99)
        monkeypatch.setattr(loader, "_REPLACE_ATTEMPTS", 3)

        with pytest.raises(PermissionError):
            write_toml(existing, {"llm": {"provider": "custom:gpu"}})

        assert existing.read_text(encoding="utf-8") == _BEFORE
        assert [p.name for p in existing.parent.iterdir()] == ["providers.toml"]
