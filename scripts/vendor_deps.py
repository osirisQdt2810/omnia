#!/usr/bin/env python3
"""Vendor ``requirements/requirements-vendor.txt`` into the repo-root ``vendor/universal``.

Anki does not pip-install for users, so runtime deps are bundled with the add-on and added
to ``sys.path`` at startup. Every vendored dep is **pure-Python and cross-platform** so a
single ``.ankiaddon`` works on both macOS and Windows — there is no per-OS binary anymore.

Pydantic v1 has a pure-Python core, but pip prefers its compiled wheel when one exists, so
this installs it with ``--no-binary pydantic`` to force the pure-Python build. After the
install, any stray ``*.so``/``*.pyd`` and ``__pycache__`` are stripped as a safety net.

Usage:
    python scripts/vendor_deps.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "vendor"
UNIVERSAL_DIR = VENDOR_DIR / "universal"
REQ_FILE = REPO_ROOT / "requirements" / "requirements-vendor.txt"

# Packages forced to their pure-Python build (pip otherwise prefers a compiled wheel).
PURE_PYTHON_ONLY = ("pydantic",)


def _has_real_requirements() -> bool:
    if not REQ_FILE.exists():
        return False
    return any(
        line.strip() and not line.strip().startswith("#")
        for line in REQ_FILE.read_text(encoding="utf-8").splitlines()
    )


def _pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


def _strip_native(root: Path) -> None:
    """Remove any compiled artifacts + bytecode caches (must stay pure-Python)."""
    for pattern in ("*.so", "*.pyd"):
        for binary in root.rglob(pattern):
            binary.unlink()
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def vendor() -> None:
    if not _has_real_requirements():
        print("requirements-vendor.txt has no packages — nothing to vendor.")
        return

    # Start clean so removed deps don't linger.
    shutil.rmtree(UNIVERSAL_DIR, ignore_errors=True)
    UNIVERSAL_DIR.mkdir(parents=True, exist_ok=True)
    _pip(
        "install",
        "--no-compile",
        "--upgrade",
        "--no-binary",
        ",".join(PURE_PYTHON_ONLY),
        "--target",
        str(UNIVERSAL_DIR),
        "-r",
        str(REQ_FILE),
    )
    _strip_native(UNIVERSAL_DIR)
    leftover = [
        str(p) for pattern in ("*.so", "*.pyd") for p in UNIVERSAL_DIR.rglob(pattern)
    ]
    if leftover:
        raise SystemExit(f"Binary artifacts remain after strip: {leftover}")
    print(f"Vendored pure-Python deps into {UNIVERSAL_DIR}")


if __name__ == "__main__":
    vendor()
