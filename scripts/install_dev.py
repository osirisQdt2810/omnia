#!/usr/bin/env python3
"""Symlink ``src/omnia`` into the local Anki ``addons21`` folder for live development.

Edits to ``src/omnia`` are then picked up the next time Anki restarts — no rebuild.
Cross-platform: resolves the Anki data dir on macOS, Windows, and Linux.

Usage:
    python scripts/install_dev.py            # symlink (default)
    python scripts/install_dev.py --copy     # copy instead of symlink (Windows fallback)
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


def install(copy: bool) -> None:
    """Link (or copy) the add-on source into ``addons21``."""
    if not ADDON_DIR.is_dir():
        raise SystemExit(f"Add-on source not found: {ADDON_DIR}")

    target = anki_addons_dir() / ADDON_FOLDER_NAME
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink() or target.exists():
        if target.is_symlink() or target.is_dir():
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)

    if copy:
        shutil.copytree(ADDON_DIR, target)
        print(f"Copied {ADDON_DIR} -> {target}")
    else:
        target.symlink_to(ADDON_DIR, target_is_directory=True)
        print(f"Symlinked {target} -> {ADDON_DIR}")
    print("Restart Anki, then open Tools → Omnia.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy instead of symlink (use if symlinks are unavailable, e.g. some Windows setups)",
    )
    install(parser.parse_args().copy)
