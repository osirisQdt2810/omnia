"""Tests for ``scripts/sync_to_anki.py`` — the one command a fresh clone runs.

The subprocess-driving parts (pip, venv, git) are not worth faking; what is worth pinning is
the logic that decides whether to run them at all. Each of these checks reports on something a
clone can plausibly get wrong, and each wrong answer looks like an Omnia bug to whoever hits
it: an interpreter Anki cannot use, a clone with no vendored deps (every plugin import dies on
``No module named 'pydantic'``), or Git LFS pointers mistaken for real voice weights.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_to_anki  # noqa: E402


class TestTheInterpreterCheck:
    def test_the_running_interpreter_passes(self):
        """The suite itself runs on a supported interpreter, so this must agree."""
        assert sync_to_anki.check_python() is True

    def test_older_than_ankis_minimum_is_refused(self, monkeypatch, capsys):
        """3.9 is below Anki's minimum: the add-on's own code would not import, and letting
        the install 'succeed' would surface as a mystery failure inside Anki instead."""
        monkeypatch.setattr(sync_to_anki.sys, "version_info", (3, 9, 18))

        assert sync_to_anki.check_python() is False
        assert "3.10" in capsys.readouterr().out

    def test_the_minimum_itself_is_accepted(self, monkeypatch):
        monkeypatch.setattr(sync_to_anki.sys, "version_info", (3, 10, 0))

        assert sync_to_anki.check_python() is True


class TestTheVendoredDepsCheck:
    def test_a_populated_vendor_tree_needs_no_action(
        self, monkeypatch, tmp_path, capsys
    ):
        (tmp_path / "vendor" / "universal" / "pydantic").mkdir(parents=True)
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            sync_to_anki, "_run", lambda *a, **k: pytest.fail("must not re-vendor")
        )

        assert sync_to_anki.ensure_vendor() is True
        assert "vendor/universal" in capsys.readouterr().out

    def test_an_empty_vendor_tree_triggers_re_vendoring(self, monkeypatch, tmp_path):
        """A sparse or partial clone. Anki never pip-installs, so an add-on shipped without
        these dies at its first third-party import."""
        (tmp_path / "vendor" / "universal").mkdir(parents=True)
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        ran = []
        monkeypatch.setattr(
            sync_to_anki, "_run", lambda cmd, **k: ran.append(cmd) or True
        )

        assert sync_to_anki.ensure_vendor() is True
        assert any("vendor_deps.py" in str(part) for part in ran[0])

    def test_a_missing_vendor_dir_also_triggers_re_vendoring(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        ran = []
        monkeypatch.setattr(
            sync_to_anki, "_run", lambda cmd, **k: ran.append(cmd) or True
        )

        assert sync_to_anki.ensure_vendor() is True
        assert ran

    def test_the_bin_dir_alone_does_not_count_as_vendored(self, monkeypatch, tmp_path):
        """``vendor/universal/bin`` holds console scripts, not importable packages — a tree
        with only that in it cannot satisfy a single import."""
        (tmp_path / "vendor" / "universal" / "bin").mkdir(parents=True)
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        ran = []
        monkeypatch.setattr(
            sync_to_anki, "_run", lambda cmd, **k: ran.append(cmd) or True
        )

        assert sync_to_anki.ensure_vendor() is True
        assert ran, "a bin-only vendor tree should have been re-vendored"

    def test_a_failed_re_vendor_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sync_to_anki, "_run", lambda *a, **k: False)

        assert sync_to_anki.ensure_vendor() is False


class TestTheVoiceModelReport:
    """Never fatal — a pointer file is fine, because the add-on downloads the voice on first
    use. It only has to say which of the two it is looking at."""

    def _voice(self, tmp_path: Path, content: bytes) -> Path:
        piper = tmp_path / "models" / "piper"
        piper.mkdir(parents=True)
        weight = piper / "vi_VN-vais1000-medium.onnx"
        weight.write_bytes(content)
        return weight

    def test_a_real_weight_is_reported_as_present(self, monkeypatch, tmp_path, capsys):
        self._voice(tmp_path, b"\x08\x01\x12\x00 not a pointer, binary onnx")
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)

        sync_to_anki.report_voice_models()

        assert "voice weight(s) present" in capsys.readouterr().out

    def test_an_lfs_pointer_is_named_as_such(self, monkeypatch, tmp_path, capsys):
        self._voice(
            tmp_path,
            b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 63201379\n",
        )
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)

        sync_to_anki.report_voice_models()

        out = capsys.readouterr().out
        assert "Git LFS pointers" in out
        assert "git lfs pull" in out

    def test_no_weights_at_all_is_still_fine(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "models" / "piper").mkdir(parents=True)
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)

        sync_to_anki.report_voice_models()

        assert "fetched on first use" in capsys.readouterr().out

    def test_only_the_prefix_is_read(self, monkeypatch, tmp_path):
        """A real weight is ~60 MB. Reading the whole file to inspect 23 bytes would make a
        status line the most expensive step of the install."""
        weight = self._voice(tmp_path, b"\x08\x01" + b"\x00" * 4096)
        monkeypatch.setattr(sync_to_anki, "REPO_ROOT", tmp_path)
        sizes = []
        real_open = Path.open

        def _spy(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self == weight:
                original_read = handle.read

                def _read(size=-1):
                    sizes.append(size)
                    return original_read(size)

                handle.read = _read
            return handle

        monkeypatch.setattr(Path, "open", _spy)
        sync_to_anki.report_voice_models()

        assert sizes and all(0 < size <= 64 for size in sizes), sizes


class TestIsAnkiRunning:
    """Only picks the closing line ('restart Anki' vs 'start Anki') — but a check that is
    always True tells every user to quit an app they never opened, which reads as the script
    misunderstanding their machine."""

    def _fake_run(self, monkeypatch, answers: dict[str, str]):
        """Answer each probe by its last argument; record what was asked."""
        asked = []

        class _Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        def _run(cmd, **_kwargs):
            asked.append(cmd)
            return _Result(answers.get(cmd[-1], ""))

        monkeypatch.setattr(sync_to_anki.subprocess, "run", _run)
        return asked

    def test_posix_does_not_match_the_script_itself(self, monkeypatch):
        """``pgrep -f anki`` matched this very process: the script is called sync_to_anki.py,
        usually under a repo path containing 'anki'. Nothing may match the whole command line
        on a bare 'anki'."""
        monkeypatch.setattr(sync_to_anki.sys, "platform", "linux")
        asked = self._fake_run(monkeypatch, {})

        assert sync_to_anki.anki_is_running() is False
        assert ["pgrep", "-f", "anki"] not in asked

    def test_posix_finds_the_classic_binary_by_exact_name(self, monkeypatch):
        monkeypatch.setattr(sync_to_anki.sys, "platform", "darwin")
        self._fake_run(monkeypatch, {"anki": "4711\n"})

        assert sync_to_anki.anki_is_running() is True

    def test_posix_finds_the_launcher_build(self, monkeypatch):
        """The launcher runs …/AnkiProgramFiles/.venv/bin/python, whose process NAME is just
        'python' — only the command line identifies it."""
        monkeypatch.setattr(sync_to_anki.sys, "platform", "darwin")
        self._fake_run(monkeypatch, {"AnkiProgramFiles": "4712\n"})

        assert sync_to_anki.anki_is_running() is True

    def test_windows_reads_the_task_list(self, monkeypatch):
        monkeypatch.setattr(sync_to_anki.sys, "platform", "win32")

        class _Result:
            stdout = "anki.exe   4713 Console   1   180,000 K\n"

        monkeypatch.setattr(sync_to_anki.subprocess, "run", lambda *a, **k: _Result())

        assert sync_to_anki.anki_is_running() is True

    def test_windows_with_no_match(self, monkeypatch):
        monkeypatch.setattr(sync_to_anki.sys, "platform", "win32")

        class _Result:
            stdout = "INFO: No tasks are running which match the specified criteria.\n"

        monkeypatch.setattr(sync_to_anki.subprocess, "run", lambda *a, **k: _Result())

        assert sync_to_anki.anki_is_running() is False

    def test_a_missing_pgrep_is_not_fatal(self, monkeypatch):
        """A trimmed container may not have it; the install must still finish."""
        monkeypatch.setattr(sync_to_anki.sys, "platform", "linux")

        def _boom(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory: 'pgrep'")

        monkeypatch.setattr(sync_to_anki.subprocess, "run", _boom)

        assert sync_to_anki.anki_is_running() is False


class TestTheVenvInterpreterPath:
    def test_windows_layout(self, monkeypatch):
        monkeypatch.setattr(sync_to_anki.sys, "platform", "win32")

        assert sync_to_anki._venv_python(Path("repo/.venv")).parts[-2:] == (
            "Scripts",
            "python.exe",
        )

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_posix_layout(self, monkeypatch, platform):
        monkeypatch.setattr(sync_to_anki.sys, "platform", platform)

        assert sync_to_anki._venv_python(Path("repo/.venv")).parts[-2:] == (
            "bin",
            "python",
        )
