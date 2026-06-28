#!/usr/bin/env python3
"""Build a distributable ``.ankiaddon`` from ``src/omnia``.

An ``.ankiaddon`` is a zip whose members live at the archive root (``__init__.py`` at the
top, not nested under a folder). This script zips the *contents* of ``src/omnia`` and
writes ``dist/omnia.ankiaddon``.

Usage:
    python scripts/build_addon.py
"""

from __future__ import annotations

import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "src" / "omnia"
DIST_DIR = REPO_ROOT / "dist"
OUTPUT = DIST_DIR / "omnia.ankiaddon"

# Patterns never shipped inside the add-on.
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {
    ".DS_Store",
    "meta.json",
}  # meta.json is created by Anki at install time


def _should_skip(path: Path) -> bool:
    """Return True if ``path`` must not be included in the package."""
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return path.name in EXCLUDE_NAMES


def build() -> Path:
    """Zip ``src/omnia`` contents into ``dist/omnia.ankiaddon`` and return the path."""
    if not ADDON_DIR.is_dir():
        raise SystemExit(f"Add-on source not found: {ADDON_DIR}")

    DIST_DIR.mkdir(exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()

    count = 0
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ADDON_DIR.rglob("*")):
            if path.is_dir() or _should_skip(path):
                continue
            zf.write(path, path.relative_to(ADDON_DIR).as_posix())
            count += 1

    print(f"Built {OUTPUT} ({count} files)")
    return OUTPUT


if __name__ == "__main__":
    build()
