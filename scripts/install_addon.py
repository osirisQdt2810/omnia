#!/usr/bin/env python3
"""Assemble the add-on into the local Anki ``addons21`` folder for live development.

The repo is source-only under ``src/omnia/`` with the non-source data (vendored deps, voice
models, config templates) at the repo root. A deployed Anki add-on, however, needs everything
side-by-side in ONE folder, so this script *assembles* that folder by:

* symlinking each source item (``__init__.py``, ``envs.py``, ``manifest.json``, ``core``,
  ``gui``, ``plugins``) from ``src/omnia`` into the target — edits are picked up on the next
  Anki restart, no rebuild;
* symlinking the repo-root sibling data dirs (``vendor``, ``models``, and ``config`` — the
  shipped ``*.example.toml`` templates) into the target;
* creating the runtime dir ``user_files`` (with its live ``config`` + ``config/.secrets``
  subdirs) as REAL directories in the target, only if absent (never clobbering user data on
  re-run); and
* seeding the secrets README into ``user_files/config/.secrets`` if missing (live ``*.toml``
  are NEVER seeded — the add-on writes them under ``user_files/config`` on first run).

The LIVE config + secrets live under ``user_files/`` so Anki preserves them across add-on
updates; the root ``config/`` is templates-only (refreshed on every update).

Because each top-level item is symlinked individually (not the whole package folder), the
add-on's ``__init__.py`` resolves its directory — not the file — so the runtime siblings it
needs (``vendor``, ``models``, ``config`` templates, ``user_files``) live next to it in the
assembled folder rather than back in ``src/omnia``.

Where the OS refuses a symlink — Windows without Developer Mode or an elevated shell — each
item falls back on its own: a directory becomes a **junction** (no privilege needed, and just
as live as a symlink) and the few top-level files are **copied**, so a stock Windows box gets
a working install and the run prints which items are snapshots rather than links.

Usage:
    python scripts/install_addon.py            # link-assemble (default)
    python scripts/install_addon.py --copy     # copy everything (a snapshot, not a live view)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from common import enable_utf8_output

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "src" / "omnia"
ADDON_FOLDER_NAME = "omnia"  # dev folder name inside addons21/

# Source items linked individually from src/omnia (the source-only package).
SOURCE_ITEMS = ("__init__.py", "envs.py", "manifest.json", "core", "gui", "plugins")
# Repo-root data dirs linked in as siblings of the source items. ``config`` is the shipped
# TEMPLATE dir (its ``*.example.toml``); the LIVE config lives under ``user_files/config``.
SIBLING_LINKS = {
    "vendor": REPO_ROOT / "vendor",
    "models": REPO_ROOT / "models",
    "config": REPO_ROOT / "config",
}
# Runtime dirs created as REAL dirs in the target, only if absent (hold user data preserved
# across add-on updates). The live config + secrets live under ``user_files/config``.
RUNTIME_DIRS = ("user_files", "user_files/config", "user_files/config/.secrets")


def anki_base_dir() -> Path:
    """Return the Anki base folder this machine's Anki actually uses.

    ``ANKI_BASE`` wins, because Anki itself honours it: pointing Anki at another base folder and
    then installing the add-on into the default one leaves you debugging an add-on that is not
    loaded. It is the normal way to run a throwaway profile — a second Anki with its own
    collection, for testing something that writes to notes — so the install script has to agree
    with the app rather than assume the default.
    """
    override = os.environ.get("ANKI_BASE")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Anki2"
    # Branch on sys.platform rather than os.name: it says the same thing here, and it is the one
    # a test can substitute — pathlib picks PosixPath/WindowsPath off os.name, so patching THAT
    # makes every Path in the process try to be a WindowsPath on a Mac.
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Anki2" if appdata else Path.home() / "Anki2"
    return Path.home() / ".local" / "share" / "Anki2"


def anki_addons_dir() -> Path:
    """Return the platform-specific Anki ``addons21`` directory."""
    return anki_base_dir() / "addons21"


# What each item fell back to when the OS refused a symlink, reported at the end of a run so
# a Windows dev knows which items are live-linked and which are snapshots.
_FALLBACKS: list[str] = []


def _is_link(path: Path) -> bool:
    """True if ``path`` is a symlink OR a Windows junction — both are unlinked, not walked.

    ``Path.is_symlink()`` alone is not enough: a junction is a reparse point CPython reports as
    ``is_symlink() == False`` and ``is_dir() == True``, so the clear-out below would take it for
    a real directory and hand it to :func:`shutil.rmtree` — which refuses ("Cannot call rmtree
    on a symbolic link") and aborts the run. The install would then be a one-shot: fine the
    first time, broken on every re-run. ``os.readlink`` succeeds on a junction (3.8+), which is
    the portable way to ask.
    """
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
    except OSError:
        return False
    return True


def _remove_link(path: Path) -> None:
    """Remove a link entry without following it — ``rmdir`` for a dir link, ``unlink`` else."""
    if path.is_dir():
        os.rmdir(path)  # a dir symlink / junction: drops the entry, keeps the target
    else:
        path.unlink()


def _clear_prior_assembly(target: Path) -> None:
    """Remove a prior whole-folder symlink or assembled dir, preserving runtime data.

    A re-run must re-link the source/sibling items without destroying the user's runtime data
    (``config``/``.secrets``/``user_files``). So: a whole-folder symlink (an older
    ``install_dev`` layout) is unlinked; an assembled dir has only its source/sibling LINKS
    removed, leaving the real runtime dirs (and their contents) in place.

    Args:
        target: The add-on folder to clear before reassembly.
    """
    if _is_link(target):
        _remove_link(target)
        return
    if not target.is_dir():
        return
    for name in (*SOURCE_ITEMS, *SIBLING_LINKS):
        item = target / name
        if _is_link(item):
            _remove_link(item)
        elif item.is_dir():
            # A prior --copy run left a real dir; remove it so the link/copy is fresh.
            shutil.rmtree(item)
        elif item.exists():
            item.unlink()


def _create_junction(src: Path, dest: Path) -> None:
    """Create a Windows directory junction ``dest`` -> ``src`` (needs no special privilege)."""
    try:
        import _winapi

        _winapi.CreateJunction(str(src), str(dest))
    except (ImportError, AttributeError, OSError):
        # Fall back to the shell built-in, which does the same thing.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
            check=True,
            capture_output=True,
        )


def _place(src: Path, dest: Path, *, copy: bool) -> None:
    """Symlink (or copy) ``src`` to ``dest``.

    On Windows, creating a symlink needs either Developer Mode or an elevated shell; without
    one, ``os.symlink`` raises ``OSError`` (WinError 1314) and the whole install used to abort.
    It falls back instead — a **junction** for a directory (no privilege required, and still a
    live view of the repo, so ``core``/``gui``/``plugins`` edits are picked up exactly as a
    symlink would) and a **copy** for the handful of top-level files (junctions are
    directory-only). Copied files are re-copied on every run, so a re-run after editing one is
    all that is needed.

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
        return
    try:
        dest.symlink_to(src, target_is_directory=src.is_dir())
    except OSError:
        if not sys.platform.startswith("win"):
            raise
        if src.is_dir():
            _create_junction(src, dest)
            _FALLBACKS.append(f"{dest.name}/ -> junction (live, same as a symlink)")
        else:
            shutil.copy2(src, dest)
            _FALLBACKS.append(
                f"{dest.name} -> copied (re-run this script after editing it)"
            )


def _seed_runtime(target: Path) -> None:
    """Seed the secrets README into the live secrets dir if missing.

    Config TEMPLATES ship at the add-on root ``config/`` (a symlink of the repo ``config/``),
    so they are never copied here. Live ``*.toml`` are never seeded either — the add-on writes
    ``providers.toml`` under ``user_files/config`` itself on first run. This only drops the
    secrets README next to where the live ``.secrets/`` will be, and only when absent.

    Args:
        target: The assembled add-on folder whose live secrets dir is seeded.
    """
    readme_src = REPO_ROOT / "config" / "secrets.README.md"
    readme_dst = target / "user_files" / "config" / ".secrets" / "README.md"
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

    verb = "Copied" if copy else ("Assembled" if _FALLBACKS else "Symlinked")
    print(f"{verb} the add-on into {target}")
    if _FALLBACKS:
        print(
            "\nThis OS refused symlinks (Windows needs Developer Mode or an elevated shell),\n"
            "so these items were placed another way:"
        )
        for note in _FALLBACKS:
            print(f"  - {note}")
        _FALLBACKS.clear()
    print("Restart Anki, then open Tools → Omnia.")
    return target


if __name__ == "__main__":
    enable_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy instead of symlink (use if symlinks are unavailable, e.g. some Windows setups)",
    )
    install(parser.parse_args().copy)
