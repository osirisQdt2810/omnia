"""Omnia — all-in-one pluginized Anki add-on. Entry point.

Import-safe headless: at import time it only sets up the vendor path and registers the
feature plugins (pure class definitions). All Anki work happens inside hook callbacks, so
``import omnia.core.*`` works under the test stubs without a running Anki.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_ADDON_DIR = Path(__file__).resolve().parent

# Anki loads an add-on under its *folder* name (the numeric AnkiWeb id once published, e.g.
# "123456"), so this package's __name__ is that id, not "omnia". Alias "omnia" to this
# package so the absolute ``from omnia... import`` statements below resolve regardless of the
# install folder name. (In dev the folder IS "omnia", so this is a harmless no-op.)
sys.modules.setdefault("omnia", sys.modules[__name__])


def _add_vendor_paths() -> None:
    """Make vendored deps importable from ``vendor/universal``.

    Every vendored dep is pure-Python and cross-platform now (pydantic v1, tomli_w, rsa,
    pyasn1), so there is one ``universal`` dir and no per-OS binary subdir to pick.

    APPEND (not insert) so Anki's own packages win for any overlap and vendor only fills the
    gaps. Back-compat: if there's no ``universal`` subdir (older flat layout), add the vendor
    dir itself so the add-on still imports.
    """
    vendor = _ADDON_DIR / "vendor"
    if not vendor.is_dir():
        return
    universal = vendor / "universal"
    target = universal if universal.is_dir() else vendor
    if str(target) not in sys.path:
        sys.path.append(str(target))


_add_vendor_paths()

# Importing the plugins package runs each plugin's @register at load time.
from omnia import plugins  # noqa: E402  (import for side effects: registration)
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
    # Tee any displayed/uncaught exception's full traceback into omnia.log — Anki's error
    # dialog only exposes the version + add-on list, not the traceback, so this is what makes
    # a user-reported crash diagnosable from the log alone.
    from omnia.core.diagnostics import install_crash_logger
    from omnia.core.logging import get_logger

    install_crash_logger(get_logger())
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
