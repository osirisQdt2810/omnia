"""Omnia — all-in-one pluginized Anki add-on. Entry point.

Import-safe headless: at import time it only sets up the vendor path and registers the
feature plugins (pure class definitions). All Anki work happens inside hook callbacks, so
``import omnia.core.*`` works under the test stubs without a running Anki.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from omnia.core.providers.voice_cache import VoiceCache

# Resolve the DIRECTORY, not the file: with the per-item-symlink deploy, ``__init__.py`` is a
# symlink back into the repo's ``src/omnia``. Resolving the file would follow it there (where
# the runtime siblings vendor/, models/, config/, user_files/ do NOT exist); resolving the
# parent instead keeps ``_ADDON_DIR`` at the real add-on folder where those siblings are
# assembled. The root config/ holds only the shipped *.example.toml templates (refreshed on
# every add-on update); the LIVE config + secrets live under user_files/config/ (+ its
# .secrets/), which Anki preserves across updates.
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
from omnia.core.config import ConfigRepository, SecretsStore  # noqa: E402
from omnia.core.logging import setup_logging  # noqa: E402
from omnia.core.manager import PluginManager  # noqa: E402
from omnia.core.plugin import AddonPaths  # noqa: E402

_manager: Optional[PluginManager] = None
# The single Tools → Omnia QAction. Tracked so it can be removed on profile close and not
# re-appended on the next profile open (profile_did_open fires on every profile switch).
_menu_action: Optional[Any] = None
# The active fetched-voice cache resolved by the PersistenceDispatcher at bootstrap (the backend
# selected by OMNIA_VOICE_CACHE_STORAGE). The Smart Notes dialog reads it via
# active_voice_cache(); resolved once so the sync-on-change runs at startup, not per dialog open.
_voice_cache: Optional[VoiceCache] = None


def addon_user_files_dir() -> Path:
    """The add-on's ``user_files`` directory (runtime state Anki preserves across updates).

    Holds logs, the LIVE config + secrets (``user_files/config/`` + its ``.secrets/``, seeded
    on first run from the shipped templates so they survive add-on updates) and any per-plugin
    state files (usage + the fetched-voice cache live in the collection DB per ADR-006). Public
    so GUI glue can resolve it without threading a path through every constructor.
    """
    path = _ADDON_DIR / "user_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _user_files_dir() -> Path:
    return addon_user_files_dir()


def active_voice_cache() -> VoiceCache:
    """The active fetched-voice cache (the backend OMNIA_VOICE_CACHE_STORAGE selected).

    Public so the Smart Notes dialog can inject it. Resolved by the PersistenceDispatcher at
    bootstrap (so the sync-on-change runs once at startup); before bootstrap / on a headless
    import it degrades to the collection backend, whose ``col`` is resolved lazily (defaults /
    no-op without a collection).
    """
    if _voice_cache is not None:
        return _voice_cache
    from omnia.core.providers.voice_cache import CollectionVoiceCache

    return CollectionVoiceCache()


def _bootstrap() -> None:
    """Build the PluginManager from Anki config and activate enabled plugins."""
    global _manager, _voice_cache
    if _manager is not None:
        return

    user_files = _user_files_dir()
    # One env-driven dispatcher resolves every persistence concern's active backend (ADR-006):
    # it reads the OMNIA_*_STORAGE knobs and, when a knob changed since last startup, syncs that
    # concern's data from the previous backend into the new one before returning it. Built after
    # user_files is known and while col is available (profile_did_open). Imported locally so this
    # module stays import-safe headless.
    from omnia.core.config.dispatch import PersistenceDispatcher
    from omnia.core.providers import usage

    dispatcher = PersistenceDispatcher(user_files)
    # Usage records LLM/TTS calls; the DB backend BUFFERS in memory and flushes on the Qt main
    # thread, because col.db is not safe to write per-record off a bg generation thread.
    usage.set_default_recorder(dispatcher.usage_recorder())
    # The fetched-voice cache the Smart Notes dialog reads via active_voice_cache().
    _voice_cache = dispatcher.voice_cache()
    # config/ at the add-on root ships the templates (refreshed each update); the LIVE config +
    # secrets live under user_files/config/ so Anki preserves them across add-on updates.
    template_dir = _ADDON_DIR / "config"
    live_config_dir = _ADDON_DIR / "user_files" / "config"
    live_config_dir.mkdir(parents=True, exist_ok=True)
    (live_config_dir / ".secrets").mkdir(parents=True, exist_ok=True)
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
    # captured in omnia.log before we re-raise (which aborts boot, the current behaviour). The
    # loader is the backend OMNIA_CONFIG_STORAGE selected (collection by default, ADR-006):
    # plugin settings + enable flags live in the synced collection; providers.toml stays on disk.
    loader = dispatcher.config_loader(live_config_dir, template_dir=template_dir)
    try:
        repository = ConfigRepository(
            loader, secrets=SecretsStore(live_config_dir / ".secrets")
        )
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
    # Flush any buffered usage synchronously before we tear down — the coalesced main-thread
    # flush may still be pending, and this is the last safe point on the main thread.
    from omnia.core.providers import usage

    usage.flush_default_recorder()
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
