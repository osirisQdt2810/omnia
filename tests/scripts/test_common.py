"""Tests for the dev scripts' shared helpers (``scripts/common.py``).

These exist because of a real Windows failure, not a hypothetical one: ``install_addon.py``
symlinked the add-on into ``addons21`` correctly and then died with ``UnicodeEncodeError`` on
its own success message, because Windows gives ``sys.stdout`` the ANSI codepage and the message
contains ``→``. The work had already happened; only the report crashed — which is the worst
shape of failure, since a developer following the README reasonably concludes the install
failed and starts undoing it.

The first test reproduces that exact crash against a genuine cp1252 stream and shows the guard
removes it. The last one is the regression net: a script added later that prints but forgets to
arm the guard fails here rather than on someone's Windows box.
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

#: Every directory holding a standalone Python entry point that PRINTS. ``tests/smoke`` is in
#: here because ``run_smoke.py`` is one: it lives under ``tests/`` (so pytest does not collect it)
#: but it is run by hand like any script, prints non-ASCII step labels, and imports the guard from
#: ``scripts/common.py`` — so it belongs in this net, not outside it because of where it sits.
PRINTING_ENTRY_POINT_DIRS = (
    SCRIPTS_DIR,
    _REPO_ROOT / "tests" / "smoke",
    _REPO_ROOT / "tests" / "benchmarks",
)

from common import enable_utf8_output  # noqa: E402

#: The character that actually broke the Windows install run.
ARROW = "Restart Anki, then open Tools → Omnia."


def _cp1252_stream() -> io.TextIOWrapper:
    """A stream encoded exactly like a default Windows console."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


class TestEnableUtf8Output:
    def test_it_removes_the_crash_that_prompted_it(self, monkeypatch):
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)
        with pytest.raises(UnicodeEncodeError):
            stream.write(ARROW)  # the failure as it happens on Windows today

        enable_utf8_output()

        stream.write(ARROW)  # must not raise
        assert stream.encoding == "utf-8"

    def test_stderr_is_fixed_too(self, monkeypatch):
        # A traceback carrying a non-ASCII path would otherwise crash while REPORTING a crash.
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stderr", stream)

        enable_utf8_output()

        assert stream.encoding == "utf-8"

    def test_a_stream_without_reconfigure_is_left_alone(self, monkeypatch):
        """pytest's capture objects have no ``reconfigure``; neither does a plain StringIO.

        Output encoding is never the point of the script that calls this, so a stream it cannot
        adjust must be skipped silently rather than take the script down with it — the very
        failure mode this module exists to remove.
        """
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        enable_utf8_output()  # must not raise

    def test_calling_it_twice_is_harmless(self, monkeypatch):
        # Two scripts importing each other, or a re-entrant call, must not be a problem.
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)

        enable_utf8_output()
        enable_utf8_output()

        assert stream.encoding == "utf-8"


class TestEveryPrintingScriptIsArmed:
    """The regression net for printing entry points added after this fix.

    Checked structurally rather than by running each one: they symlink into Anki, build archives,
    shell out to pip, or need Anki's own interpreter, so *executing* them in the suite is not an
    option — but "forgot to arm it" is precisely the mistake that recurs, and it is visible in the
    source. Scope is :data:`PRINTING_ENTRY_POINT_DIRS`, not ``scripts/`` alone: a hand-run entry
    point is in scope because of what it does, not because of which folder it sits in.
    """

    @staticmethod
    def _scripts_that_print() -> list[Path]:
        found = []
        candidates = [
            path
            for directory in PRINTING_ENTRY_POINT_DIRS
            for path in sorted(directory.glob("*.py"))
        ]
        for path in candidates:
            if path.name == "common.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            prints = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                for node in ast.walk(tree)
            )
            if prints:
                found.append(path)
        return found

    def test_the_scan_actually_finds_scripts(self):
        # Guards against the check below passing vacuously if the glob or the AST walk breaks.
        assert len(self._scripts_that_print()) >= 5

    @pytest.mark.parametrize(
        "script", _scripts_that_print.__func__(), ids=lambda p: p.name
    )
    def test_it_arms_the_guard(self, script: Path):
        source = script.read_text(encoding="utf-8")
        assert (
            "from common import enable_utf8_output" in source
        ), f"{script.name} prints but never imports the guard"
        assert (
            "enable_utf8_output()" in source
        ), f"{script.name} imports the guard but never calls it"
