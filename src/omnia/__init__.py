"""Omnia — all-in-one pluginized Anki add-on. Entry point.

Import-safe headless: at import time it only sets up the vendor path and registers the
feature plugins (pure class definitions). All Anki work happens inside hook callbacks, so
``import omnia.core.*`` works under the test stubs without a running Anki.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

# Resolve the DIRECTORY, not the file: with the per-item-symlink deploy, ``__init__.py`` is a
# symlink back into the repo's ``src/omnia``. Resolving the file would follow it there (where
# the runtime siblings vendor/, models/, config/, .secrets/ do NOT exist); resolving the parent
# instead keeps ``_ADDON_DIR`` at the real add-on folder where those siblings are assembled.
_ADDON_DIR = Path(__file__).parent.resolve(strict=False)

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
# The single Tools → Omnia QAction. Tracked so it can be removed on profile close and not
# re-appended on the next profile open (profile_did_open fires on every profile switch).
_menu_action: Optional[Any] = None


def addon_user_files_dir() -> Path:
    """The add-on's ``user_files`` directory (runtime state Anki preserves across updates).

    Holds logs + per-plugin state files (logs, usage.json, the fetched-voice cache) — NOT
    config. Public so GUI glue (e.g. the Smart Notes dialog's voice cache) can resolve it
    without threading a path through every constructor.
    """
    path = _ADDON_DIR / "user_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _user_files_dir() -> Path:
    return addon_user_files_dir()


def _bootstrap() -> None:
    """Build the PluginManager from Anki config and activate enabled plugins."""
    global _manager
    if _manager is not None:
        return

    user_files = _user_files_dir()
    # Every ProviderHub built anywhere records LLM/TTS usage to user_files/usage.json (Anki
    # preserves user_files across updates; .gitignore already excludes it). Imported locally
    # so the module stays import-safe headless.
    from omnia.core.providers import usage

    usage.set_default_recorder(usage.JsonUsageRecorder(user_files / "usage.json"))
    config_dir = _ADDON_DIR / "config"
    (_ADDON_DIR / ".secrets").mkdir(parents=True, exist_ok=True)
    paths = AddonPaths(
        addon_dir=_ADDON_DIR,
        web_dir=_ADDON_DIR / "web",
        user_files_dir=user_files,
    )
    logger = setup_logging(user_files)
    # Tee any displayed/uncaught exception's full traceback into omnia.log — Anki's error
    # dialog only exposes the version + add-on list, not the traceback, so this is what makes
    # a user-reported crash diagnosable from the log alone.
    from omnia.core.logging import install_crash_logger

    install_crash_logger(logger)
    # Build the repository AFTER logging is up: it eager-loads + validates the config (extra
    # keys forbidden), so a config typo raises here — logging first guarantees that failure is
    # captured in omnia.log before we re-raise (which aborts boot, the current behaviour).
    loader = ConfigLoader(config_dir)
    try:
        repository = ConfigRepository(loader)
    except Exception:
        logger.exception("Failed to load Omnia config")
        raise
    _manager = PluginManager(repository, paths)
    _manager.setup()
    _install_menu()


def _install_menu() -> None:
    """Add the single Tools → Omnia entry that opens the settings dialog.

    Idempotent across profile reloads: ``profile_did_open`` fires on every profile switch, so
    any previously-installed action is removed first — otherwise a duplicate "Omnia" item was
    appended to the Tools menu on each switch.
    """
    global _menu_action
    from aqt import mw
    from aqt.qt import QAction

    if _menu_action is not None:
        mw.form.menuTools.removeAction(_menu_action)
        _menu_action = None
    action = QAction("Omnia", mw)
    action.triggered.connect(_open_settings)
    mw.form.menuTools.addAction(action)
    _menu_action = action


def _open_settings() -> None:
    from aqt import mw

    from omnia.gui.settings_dialog import SettingsDialog

    assert _manager is not None
    SettingsDialog(_manager, mw).exec()


def _teardown() -> None:
    global _manager, _menu_action
    if _menu_action is not None:
        from aqt import mw

        mw.form.menuTools.removeAction(_menu_action)
        _menu_action = None
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
