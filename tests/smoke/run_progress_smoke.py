"""Does the "Omnia: installing…" modal actually retitle, against a REAL ProgressManager?

This is the check behind the "stuck at Omnia installing…" report. ``QueryOp.with_progress``
takes ONE label and shows it for the whole run, so a clipper or native-runtime install — a
clone, a venv, a few hundred MB of pip, a PyInstaller freeze — sat behind one unchanging line
for minutes and read as hung. ``anki_compat.update_progress`` is what lets the step-by-step
progress reach that modal.

It needs a real ``ProgressManager`` to prove anything. The helper is deliberately wrapped in
``suppress(Exception)`` — a cosmetic label must never fail an install that is otherwise
succeeding — so ANY breakage here is silent, and a stubbed progress manager (what
``tests/conftest.py`` gives the unit tests) cannot tell a working retitle from a swallowed
``AttributeError``. Anki's own ``update()`` calls ``mw.inMainThread()`` and touches
version-specific widget internals; only the real thing exercises that.

Run it with Anki's bundled interpreter (see ``run_smoke.py`` for the per-platform invocation):
    "<AnkiProgramFiles>/.venv/bin/python" tests/smoke/run_progress_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.append(str(_REPO / "vendor" / "universal"))
sys.path.insert(0, str(_REPO / "scripts"))

from common import enable_utf8_output  # noqa: E402

enable_utf8_output()

import anki.buildinfo  # noqa: E402
import aqt  # noqa: E402
from aqt.progress import ProgressManager  # noqa: E402
from aqt.qt import QApplication, QLabel, QMainWindow  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from omnia.core import anki_compat  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("OK   " if ok else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class Window(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.app = app
        self.pm = SimpleNamespace(name="Probe")

    # ProgressManager calls these around modal work. ``inMainThread`` is not optional: Anki's
    # own ``update()`` calls it before touching the widget, and ``update_progress`` suppresses
    # exceptions — so a stand-in missing it looks exactly like the retitle not working.
    def setEnabled(self, _on: bool) -> None:
        return

    def inMainThread(self) -> bool:
        return True


def labels_of(widget) -> list[str]:
    return [child.text() for child in widget.findChildren(QLabel)]


def pump(seconds: float) -> None:
    """Let Qt run. Anki delays SHOWING a progress dialog by ~600ms and ignores label updates
    until it is up, so a probe that updates immediately measures the delay, not the retitle.
    A real install is minutes long and the modal is up throughout.
    """
    import time as _time

    deadline = _time.time() + seconds
    while _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.02)


def main() -> int:
    window = Window()
    aqt.mw = window
    window.progress = ProgressManager(window)

    print(f"anki {anki.buildinfo.version} on {sys.platform}")

    window.progress.start(
        label="Omnia: installing the desktop clipper…", immediate=True
    )
    pump(1.5)
    dialog = window.progress._win
    check("1. a progress modal is up", dialog is not None)
    if dialog is None:
        return 1
    before = labels_of(dialog)
    check(
        "2. it shows the QueryOp's one fixed label",
        any("installing the desktop clipper" in text for text in before),
        repr(before),
    )

    anki_compat.update_progress(
        "Installing dependencies (a few hundred MB — first run only)…"
    )
    pump(0.3)
    after = labels_of(dialog)
    check(
        "3. update_progress retitled it — the install no longer reads as hung",
        any("Installing dependencies" in text for text in after),
        repr(after),
    )

    anki_compat.update_progress("Building the app…")
    pump(0.3)
    check(
        "4. and again, so every step reaches the user",
        any("Building the app" in text for text in labels_of(dialog)),
        repr(labels_of(dialog)),
    )

    window.progress.finish()
    app.processEvents()
    anki_compat.update_progress("nothing is up any more")
    check("5. it is safe with no modal on screen", True, "no exception")

    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("INSTALL PROGRESS MODAL VERIFIED (5 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
