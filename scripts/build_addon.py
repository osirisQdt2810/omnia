#!/usr/bin/env python3
"""Build a distributable ``.ankiaddon`` from the source-only package + repo-root data.

An ``.ankiaddon`` is a zip whose members live at the archive root (``__init__.py`` at the top,
not nested under a folder). The repo keeps the package under ``src/omnia/`` and its non-source
data at the repo root (``vendor/``, ``models/``, ``config/``), so this script assembles them
into one archive:

* ``src/omnia/**`` at the archive root (``__init__.py`` at the top),
* ``vendor/**`` under ``vendor/`` (shipped vendored deps),
* ``models/**`` under ``models/`` (bundled TTS voice models), and
* ``config/*.example.toml`` under ``config/`` (tracked templates the add-on seeds from).

It EXCLUDES everything that must not ship: tests/requirements/docs/deploy/scripts, the live
``*.toml`` config + ``.secrets/``, ``user_files/``, caches, ``meta.json``, ``.DS_Store``.

Usage:
    python scripts/build_addon.py
"""

from __future__ import annotations

import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "src" / "omnia"
VENDOR_DIR = REPO_ROOT / "vendor"
MODELS_DIR = REPO_ROOT / "models"
CONFIG_DIR = REPO_ROOT / "config"
DIST_DIR = REPO_ROOT / "dist"
OUTPUT = DIST_DIR / "omnia.ankiaddon"

# Patterns never shipped inside the add-on.
EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".secrets",
    "user_files",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {
    ".DS_Store",
    "meta.json",  # created by Anki at install time
}


def _should_skip(path: Path) -> bool:
    """Return True if ``path`` must not be included in the package."""
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return path.name in EXCLUDE_NAMES


def _is_live_toml(path: Path) -> bool:
    """Return True if ``path`` is a LIVE config TOML (only ``*.example.toml`` may ship)."""
    return path.suffix == ".toml" and not path.name.endswith(".example.toml")


def _add_tree(zf: zipfile.ZipFile, root: Path, prefix: str) -> int:
    """Zip every shippable file under ``root`` at archive path ``prefix`` and return the count.

    Args:
        zf: The open zip archive.
        root: The directory whose contents are added.
        prefix: The archive-relative prefix (``""`` for the root, ``"vendor"`` etc. otherwise).

    Returns:
        The number of files written.
    """
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir() or _should_skip(path) or _is_live_toml(path):
            continue
        rel = path.relative_to(root).as_posix()
        arcname = f"{prefix}/{rel}" if prefix else rel
        zf.write(path, arcname)
        count += 1
    return count


def build() -> Path:
    """Assemble the add-on archive into ``dist/omnia.ankiaddon`` and return its path."""
    if not ADDON_DIR.is_dir():
        raise SystemExit(f"Add-on source not found: {ADDON_DIR}")
    if not VENDOR_DIR.is_dir():
        raise SystemExit(
            f"Vendored deps not found: {VENDOR_DIR}. A shipped add-on without vendor/ is "
            "broken — run `python scripts/vendor_deps.py` first."
        )

    DIST_DIR.mkdir(exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()

    count = 0
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        count += _add_tree(zf, ADDON_DIR, "")
        count += _add_tree(zf, VENDOR_DIR, "vendor")
        if MODELS_DIR.is_dir():
            count += _add_tree(zf, MODELS_DIR, "models")
        if CONFIG_DIR.is_dir():
            # Only the tracked templates ship (live *.toml are excluded by _is_live_toml).
            count += _add_tree(zf, CONFIG_DIR, "config")

    print(f"Built {OUTPUT} ({count} files)")
    return OUTPUT


if __name__ == "__main__":
    build()
