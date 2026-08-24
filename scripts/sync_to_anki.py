#!/usr/bin/env python3
"""One command that takes a fresh clone of this repo to a working add-on in your Anki.

    python scripts/sync_to_anki.py

That is the whole developer install. It checks what a clone needs, fixes what it can, and
assembles the add-on into Anki's ``addons21`` — so the only manual step left is restarting
Anki. Re-run it any time; it is idempotent and never touches your ``user_files`` (config,
secrets, downloaded voices).

What it does, in order:

1. **Checks the interpreter** — 3.10+ (Anki's minimum), so the add-on's own code can load.
2. **Checks the clone is complete** — ``vendor/universal`` (committed) must be there, because
   the add-on's third-party deps come from it at runtime and Anki never pip-installs. If it is
   missing (a sparse or partial clone), it re-vendors via ``vendor_deps.py``.
3. **Reports the LFS state of the piper voices** — the ``.onnx`` weights are Git LFS. Pointer
   files are HARMLESS: the add-on downloads the voice it needs on first use. It is a note, not
   an error, so a clone without ``git lfs`` still gets a working add-on.
4. **Assembles the add-on** into ``addons21/omnia`` via :mod:`install_addon` — links where the
   OS allows (edits are live), junctions + copies on a Windows box without Developer Mode.
5. **Tells you whether Anki is running**, since add-ons are only (re)loaded at startup.

Options:
    --dev        also create ``.venv`` and install the test tooling (pytest/ruff/black/mypy)
                 plus the pre-commit hooks — only needed to run the suite or to commit.
    --copy       copy instead of linking (a snapshot; re-run this script after every edit).
    --submodules fetch the companion clippers under ``3rdparty/`` (not needed by the add-on).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from common import enable_utf8_output
from install_addon import anki_base_dir, install

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 10)
# A Git LFS pointer is a small text file starting with this line; the real weights are binary.
LFS_POINTER_PREFIX = b"version https://git-lfs"


def _run(command: list[str], *, why: str) -> bool:
    """Run ``command`` in the repo, streaming its output. Returns True on success."""
    print(f"\n> {' '.join(command)}   ({why})")
    try:
        subprocess.run(command, cwd=REPO_ROOT, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  ! failed: {exc}")
        return False
    return True


def check_python() -> bool:
    """Refuse an interpreter older than Anki's minimum, which cannot run the add-on's code."""
    if sys.version_info < MIN_PYTHON:
        print(
            f"ERROR: Python {'.'.join(map(str, MIN_PYTHON))}+ required "
            f"(this is {sys.version.split()[0]}). Anki bundles 3.13."
        )
        return False
    print(f"OK  python {sys.version.split()[0]}")
    return True


def ensure_vendor() -> bool:
    """Make sure ``vendor/universal`` holds the runtime deps; re-vendor if the clone lacks it.

    These are committed, so a normal clone already has them — this only rescues a partial or
    sparse clone. An add-on shipped without them fails at the first ``import pydantic``.
    """
    universal = REPO_ROOT / "vendor" / "universal"
    packages = (
        [p.name for p in universal.iterdir() if not p.name.startswith(("bin", "."))]
        if universal.is_dir()
        else []
    )
    if packages:
        print(f"OK  vendor/universal ({len(packages)} entries)")
        return True
    print("!   vendor/universal is empty — re-vendoring the runtime deps")
    return _run(
        [sys.executable, str(REPO_ROOT / "scripts" / "vendor_deps.py")],
        why="the add-on's deps must be vendored; Anki never pip-installs",
    )


def _starts_with(path: Path, prefix: bytes) -> bool:
    """True if ``path`` begins with ``prefix``, reading only that many bytes."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(prefix)) == prefix
    except OSError:
        return False


def report_voice_models() -> None:
    """Say whether the piper weights are real files or LFS pointers — never fail on it."""
    piper = REPO_ROOT / "models" / "piper"
    weights = sorted(piper.glob("*.onnx")) if piper.is_dir() else []
    if not weights:
        print("--  models/piper: no .onnx weights (fetched on first use)")
        return
    # Read only the prefix: a real weight is ~60 MB, and slurping each one to look at 23 bytes
    # would make a status line cost more memory than the whole install.
    pointers = [w for w in weights if _starts_with(w, LFS_POINTER_PREFIX)]
    if pointers:
        print(
            f"--  models/piper: {len(pointers)}/{len(weights)} weights are Git LFS pointers.\n"
            "    Fine to leave: Omnia downloads a voice on first use. Run `git lfs pull`\n"
            "    only if you want them available offline right away."
        )
    else:
        print(f"OK  models/piper: {len(weights)} voice weight(s) present")


def _venv_python(venv: Path) -> Path:
    """The interpreter inside ``venv`` on this platform."""
    if sys.platform.startswith("win"):
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_dev_tooling() -> bool:
    """Create ``.venv`` if absent and install the test tooling + pre-commit hooks into it.

    Into a venv rather than the active interpreter, because the dev environment deliberately
    MIRRORS Anki's bundled runtime: it holds test tooling only, never a third-party runtime
    dep of the add-on. Those come solely from ``vendor/universal`` at runtime, and a pip copy
    sitting in front of the vendored one would let the suite pass on a package the shipped
    add-on does not actually carry.
    """
    venv = REPO_ROOT / ".venv"
    if not venv.is_dir() and not _run(
        [sys.executable, "-m", "venv", str(venv)],
        why="the project's dev virtualenv",
    ):
        return False
    python = _venv_python(venv)
    if not python.exists():
        print(f"  ! no interpreter at {python}")
        return False
    ok = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-q",
            "-r",
            str(REPO_ROOT / "requirements" / "requirements-dev.txt"),
        ],
        why="pytest / ruff / black / mypy",
    )
    if ok:
        # A pre-commit that will not install is not fatal to the add-on install — say so and
        # carry on, so a dev still ends up with a working Anki.
        _run([str(python), "-m", "pre_commit", "install"], why="git hooks")
        activate = (
            r".venv\Scripts\activate"
            if sys.platform.startswith("win")
            else "source .venv/bin/activate"
        )
        print(f"    dev env ready — activate it with:  {activate}")
    return ok


def fetch_submodules() -> bool:
    return _run(
        ["git", "submodule", "update", "--init", "--recursive"],
        why="the companion clippers under 3rdparty/",
    )


def anki_is_running() -> bool:
    """Best-effort check so the final message can say 'restart' rather than 'start'."""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq anki.exe"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.lower()
            return "anki.exe" in out
        out = subprocess.run(
            ["pgrep", "-f", "anki"], capture_output=True, text=True, check=False
        ).stdout
        return bool(out.strip())
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev",
        action="store_true",
        help="also create .venv and install the test tooling + pre-commit hooks",
    )
    parser.add_argument(
        "--copy", action="store_true", help="copy instead of linking (snapshot install)"
    )
    parser.add_argument(
        "--submodules", action="store_true", help="also fetch the companion clippers"
    )
    args = parser.parse_args()

    print(f"Omnia -> Anki   (repo: {REPO_ROOT})")
    print(f"Anki base:      {anki_base_dir()}")
    print("-" * 72)

    if not check_python():
        return 1
    if not ensure_vendor():
        return 1
    report_voice_models()
    if args.submodules and not fetch_submodules():
        return 1
    if args.dev and not install_dev_tooling():
        return 1

    print("-" * 72)
    target = install(copy=args.copy)

    print("-" * 72)
    if anki_is_running():
        print("Anki is RUNNING — quit and reopen it to load this build.")
    else:
        print("Start Anki to load this build.")
    print(f"Then: Tools -> Omnia.   (installed at {target})")
    return 0


if __name__ == "__main__":
    enable_utf8_output()
    raise SystemExit(main())
