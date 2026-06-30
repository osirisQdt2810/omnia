#!/usr/bin/env python3
"""Assemble the add-on into the local Anki ``addons21`` folder for live development.

The repo is source-only under ``src/omnia/`` with the non-source data (vendored deps, voice
models, config templates) at the repo root. A deployed Anki add-on, however, needs everything
side-by-side in ONE folder, so this script *assembles* that folder by:

* symlinking each source item (``__init__.py``, ``envs.py``, ``manifest.json``, ``core``,
  ``gui``, ``plugins``) from ``src/omnia`` into the target — edits are picked up on the next
  Anki restart, no rebuild;
* symlinking the repo-root sibling data dirs (``vendor``, ``models``) into the target;
* creating the runtime dirs (``config``, ``.secrets``, ``user_files``) as REAL directories in
  the target, only if absent (never clobbering user data on re-run); and
* seeding the ``*.example.toml`` templates + the secrets README into those runtime dirs if
  they are missing (live ``*.toml`` are NEVER seeded — the add-on writes them at runtime).

Because each top-level item is symlinked individually (not the whole package folder), the
add-on's ``__init__.py`` resolves its directory — not the file — so the runtime siblings it
needs (``vendor``, ``models``, ``config``, ``.secrets``, ``user_files``) live next to it in
the assembled folder rather than back in ``src/omnia``.

Usage:
    python scripts/install_addon.py            # symlink-assemble (default)
    python scripts/install_addon.py --copy     # copy instead of symlink (Windows fallback)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "src" / "omnia"
ADDON_FOLDER_NAME = "omnia"  # dev folder name inside addons21/

# Source items linked individually from src/omnia (the source-only package).
SOURCE_ITEMS = ("__init__.py", "envs.py", "manifest.json", "core", "gui", "plugins")
# Repo-root data dirs linked in as siblings of the source items.
SIBLING_LINKS = {"vendor": REPO_ROOT / "vendor", "models": REPO_ROOT / "models"}
# Runtime dirs created as REAL dirs in the target, only if absent (hold user data).
RUNTIME_DIRS = ("config", ".secrets", "user_files")


def anki_addons_dir() -> Path:
    """Return the platform-specific Anki ``addons21`` directory."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Anki2"
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "Anki2" if appdata else Path.home() / "Anki2"
    else:
        base = Path.home() / ".local" / "share" / "Anki2"
    return base / "addons21"


def _clear_prior_assembly(target: Path) -> None:
    """Remove a prior whole-folder symlink or assembled dir, preserving runtime data.

    A re-run must re-link the source/sibling items without destroying the user's runtime data
    (``config``/``.secrets``/``user_files``). So: a whole-folder symlink (an older
    ``install_dev`` layout) is unlinked; an assembled dir has only its source/sibling LINKS
    removed, leaving the real runtime dirs (and their contents) in place.

    Args:
        target: The add-on folder to clear before reassembly.
    """
    if target.is_symlink():
        target.unlink()
        return
    if not target.is_dir():
        return
    for name in (*SOURCE_ITEMS, *SIBLING_LINKS):
        item = target / name
        if item.is_symlink():
            item.unlink()
        elif item.is_dir():
            # A prior --copy run left a real dir; remove it so the link/copy is fresh.
            shutil.rmtree(item)
        elif item.exists():
            item.unlink()


def _place(src: Path, dest: Path, *, copy: bool) -> None:
    """Symlink (or copy) ``src`` to ``dest``.

    Args:
        src: The source file or directory.
        dest: The destination path to create.
        copy: Copy instead of symlinking.
    """
    if copy:
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
    else:
        dest.symlink_to(src, target_is_directory=src.is_dir())


def _seed_runtime(target: Path) -> None:
    """Seed config templates + the secrets README into the runtime dirs if missing.

    Never seeds live ``*.toml`` (the add-on writes those itself on first run); only the tracked
    ``*.example.toml`` templates and the secrets README, and only when absent.

    Args:
        target: The assembled add-on folder whose runtime dirs are seeded.
    """
    config_src = REPO_ROOT / "config"
    config_dst = target / "config"
    for template in config_src.glob("*.example.toml"):
        dst = config_dst / template.name
        if not dst.exists():
            shutil.copy2(template, dst)
    readme_src = config_src / "secrets.README.md"
    readme_dst = target / ".secrets" / "README.md"
    if readme_src.exists() and not readme_dst.exists():
        shutil.copy2(readme_src, readme_dst)


def install(copy: bool = False, target: Path | None = None) -> Path:
    """Assemble the add-on into ``target`` (default ``addons21/omnia``); return the target.

    Args:
        copy: Copy each item instead of symlinking (use where symlinks are unavailable, e.g.
            some Windows setups).
        target: The destination add-on folder. Defaults to ``anki_addons_dir()/"omnia"``;
            pass an explicit path to assemble into a temp dir (used by tests).

    Returns:
        The assembled target directory.
    """
    if not ADDON_DIR.is_dir():
        raise SystemExit(f"Add-on source not found: {ADDON_DIR}")

    target = target or (anki_addons_dir() / ADDON_FOLDER_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    _clear_prior_assembly(target)
    target.mkdir(parents=True, exist_ok=True)

    for name in SOURCE_ITEMS:
        src = ADDON_DIR / name
        if not src.exists():
            raise SystemExit(f"Missing required source item: {src}")
        _place(src, target / name, copy=copy)

    for name, src in SIBLING_LINKS.items():
        if not src.exists():
            print(f"WARNING: optional data dir not found, skipping: {src}")
            continue
        _place(src, target / name, copy=copy)

    for name in RUNTIME_DIRS:
        (target / name).mkdir(parents=True, exist_ok=True)

    _seed_runtime(target)

    verb = "Copied" if copy else "Symlinked"
    print(f"{verb} the add-on into {target}")
    print("Restart Anki, then open Tools → Omnia.")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy instead of symlink (use if symlinks are unavailable, e.g. some Windows setups)",
    )
    install(parser.parse_args().copy)
