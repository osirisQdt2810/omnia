"""Omnia — all-in-one pluginized Anki add-on. Entry point.

Import-safe headless: at import time it only sets up the vendor path and registers the
feature plugins (pure class definitions). All Anki work happens inside hook callbacks, so
``import omnia.core.*`` works under the test stubs without a running Anki.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Optional

_ADDON_DIR = Path(__file__).resolve().parent

# Anki loads an add-on under its *folder* name (the numeric AnkiWeb id once published, e.g.
# "123456"), so this package's __name__ is that id, not "omnia". Alias "omnia" to this
# package so the absolute ``from omnia... import`` statements below resolve regardless of the
# install folder name. (In dev the folder IS "omnia", so this is a harmless no-op.)
sys.modules.setdefault("omnia", sys.modules[__name__])


def _platform_vendor_subdir() -> Optional[str]:
    """Return the per-OS vendor subdir holding the right compiled binaries, or None.

    ``pydantic_core`` ships a Rust binary that differs per OS/arch. mac arm64 and mac x86_64
    binaries share the same filename (``…-darwin.so``) so they cannot coexist in one folder —
    hence one subdir per platform, and only the matching one is added to ``sys.path``.
    """
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return "win_x64"  # Anki ships 64-bit Python on Windows
    if system == "Darwin":
        return "mac_arm64" if machine in ("arm64", "aarch64") else "mac_x64"
    if system == "Linux":
        return "linux_x64"
    return None


def _add_vendor_paths() -> None:
    """Make vendored deps importable: the pure-Python ``universal`` dir + this platform's
    binary dir.

    APPEND (not insert) so Anki's own packages win for any overlap and vendor only fills gaps
    (pydantic, pydantic_core, yaml, tomli_w, …). The platform dir is appended *before*
    ``universal`` so the correct ``pydantic_core`` binary is found ahead of anything else.
    """
    vendor = _ADDON_DIR / "vendor"
    if not vendor.is_dir():
        return
    subdir = _platform_vendor_subdir()
    candidates = []
    if subdir:
        candidates.append(vendor / subdir)
    candidates.append(vendor / "universal")
    # Back-compat: if the vendor dir was not split into universal/<os> (older layout), fall
    # back to adding it flat so the add-on still imports.
    if not (vendor / "universal").is_dir():
        candidates = [vendor]
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.append(str(path))


_add_vendor_paths()

# Importing the features package runs each plugin's @register at load time.
from omnia import features  # noqa: E402  (import for side effects: registration)
from omnia.core.config import ConfigLoader, ConfigRepository  # noqa: E402
from omnia.core.logging import setup_logging  # noqa: E402
from omnia.core.manager import PluginManager  # noqa: E402
from omnia.core.plugin import AddonPaths  # noqa: E402

_manager: Optional[PluginManager] = None


def _user_files_dir() -> Path:
    path = _ADDON_DIR / "user_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bootstrap() -> None:
    """Build the PluginManager from Anki config and activate enabled plugins."""
    global _manager
    if _manager is not None:
        return

    user_files = _user_files_dir()
    paths = AddonPaths(
        addon_dir=_ADDON_DIR,
        web_dir=_ADDON_DIR / "web",
        user_files_dir=user_files,
    )
    loader = ConfigLoader(_ADDON_DIR / "config", user_files / "omnia.toml")
    repository = ConfigRepository(loader)
    setup_logging(user_files)
    _manager = PluginManager(repository, paths)
    _manager.setup()
    _install_menu()


def _install_menu() -> None:
    """Add a Tools → Omnia entry that opens the settings dialog."""
    from aqt import mw
    from aqt.qt import QAction

    action = QAction("Omnia", mw)
    action.triggered.connect(_open_settings)
    mw.form.menuTools.addAction(action)


def _open_settings() -> None:
    from aqt import mw

    from omnia.gui.settings_dialog import SettingsDialog

    assert _manager is not None
    SettingsDialog(_manager, mw).exec()


def _teardown() -> None:
    global _manager
    if _manager is not None:
        _manager.teardown()
        _manager = None


# Register hooks when running inside Anki. Under the test stubs `aqt` exists, so the hooks
# append harmlessly; with no Anki at all, ImportError is the expected headless case.
try:
    from aqt import gui_hooks

    gui_hooks.profile_did_open.append(_bootstrap)
    gui_hooks.profile_will_close.append(_teardown)
except ImportError:
    pass
