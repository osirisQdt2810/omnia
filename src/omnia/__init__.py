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

# Make vendored deps importable. APPEND (not insert) so Anki's own packages win for any
# overlap (e.g. typing_extensions) and vendor only fills gaps (pydantic, yaml, tomli_w, ...).
_VENDOR_DIR = _ADDON_DIR / "vendor"
if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
    sys.path.append(str(_VENDOR_DIR))

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
