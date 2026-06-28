#!/usr/bin/env python3
"""Install ``requirements/requirements-vendor.txt`` into ``src/omnia/vendor``.

Vendored deps are bundled with the add-on and added to ``sys.path`` at startup, because
Anki does not pip-install for users. Only **pure-Python, cross-platform** deps belong here
(see requirements-vendor.txt). The file is empty by design today — the provider layer uses
only the standard library — so this script is a no-op until a dep is added.

Usage:
    python scripts/vendor_deps.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "src" / "omnia" / "vendor"
REQ_FILE = REPO_ROOT / "requirements" / "requirements-vendor.txt"


def _has_real_requirements() -> bool:
    """Return True if the vendor requirements file lists any non-comment specs."""
    if not REQ_FILE.exists():
        return False
    for line in REQ_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def vendor() -> None:
    """Pip-install the vendor requirements into the vendor directory."""
    if not _has_real_requirements():
        print("requirements-vendor.txt has no packages — nothing to vendor.")
        return

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-compile",
        "--upgrade",
        "--target",
        str(VENDOR_DIR),
        "-r",
        str(REQ_FILE),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"Vendored into {VENDOR_DIR}")


if __name__ == "__main__":
    vendor()
