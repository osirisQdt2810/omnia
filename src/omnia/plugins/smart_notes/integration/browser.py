"""Finding the Chrome profile a user actually uses, and where its executable lives.

Chrome numbers profile directories in creation order (``Default``, ``Profile 1``, ``Profile
16``…) and shows the user a display name that has nothing to do with it. Someone with eight
profiles opening ``chrome://extensions`` gets whichever one Chrome felt like, which for
installing an extension is the wrong one often enough to matter — so this reads Chrome's own
``Local State`` and picks the profile Chrome itself last used.

Pure logic: the parsing takes the already-decoded ``Local State`` mapping, so it unit-tests
without a browser, a filesystem or a platform.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ChromeProfile:
    """One Chrome profile: the directory Chrome addresses it by, and the name a human sees."""

    directory: str
    name: str
    last_active: float = 0.0


def pick_profile(local_state: Mapping[str, Any]) -> Optional[ChromeProfile]:
    """Return the profile Chrome most recently used, or None when it cannot be told.

    ``last_used`` is Chrome's own answer and is preferred; the ``active_time`` in
    ``info_cache`` is the fallback for a ``Local State`` that predates it or was written by a
    Chrome that exited badly.

    Args:
        local_state: The decoded contents of Chrome's ``Local State`` file.

    Returns:
        The chosen profile, or None when the file names none.
    """
    profile = local_state.get("profile")
    if not isinstance(profile, Mapping):
        return None
    cache = profile.get("info_cache")
    cache = cache if isinstance(cache, Mapping) else {}

    def _build(directory: str) -> ChromeProfile:
        meta = cache.get(directory)
        meta = meta if isinstance(meta, Mapping) else {}
        return ChromeProfile(
            directory=directory,
            name=str(meta.get("name", "") or directory),
            last_active=float(meta.get("active_time", 0) or 0),
        )

    last_used = profile.get("last_used")
    if isinstance(last_used, str) and last_used:
        return _build(last_used)
    if not cache:
        return None
    newest = max(
        cache,
        key=lambda directory: float(
            (cache.get(directory) or {}).get("active_time", 0) or 0
        ),
    )
    return _build(newest)


def chrome_user_data_dir(
    platform: str = "", home: Optional[Path] = None
) -> Optional[Path]:
    """Return Chrome's user-data directory for this platform, or None when unknown."""
    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    if platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "Google" / "Chrome" / "User Data"
    if platform.startswith("linux"):
        return home / ".config" / "google-chrome"
    return None


def read_local_state(user_data_dir: Optional[Path]) -> dict[str, Any]:
    """Read and decode Chrome's ``Local State``. Returns ``{}`` when unreadable.

    Never raises: choosing a profile is a convenience, and a Chrome that has never run (or a
    file locked by a running one) must degrade to "no preference", not to a failed install.
    """
    if user_data_dir is None:
        return {}
    path = user_data_dir / "Local State"
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def preferred_profile(platform: str = "") -> Optional[ChromeProfile]:
    """The Chrome profile this machine's Chrome last used, or None."""
    return pick_profile(read_local_state(chrome_user_data_dir(platform)))


#: Where Chrome's executable normally lives, per platform, in the order to try.
_CHROME_PATHS = {
    "darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",),
    "win": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "linux": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
    ),
}


def chrome_executable(platform: str = "") -> Optional[str]:
    """Return the path to Chrome's executable, or None when it is not where it should be.

    Knowing the real path is what lets the caller pass ``--profile-directory``; the
    ``open -a``/``start chrome`` shortcuts take a URL but not Chrome's own flags.
    """
    platform = platform or sys.platform
    key = "win" if platform.startswith("win") else platform
    for candidate in _CHROME_PATHS.get(key, ()):  # type: ignore[arg-type]
        if os.path.exists(candidate):
            return candidate
    return None


#: Where Chrome records an installed extension. ``Secure Preferences`` is the file that holds
#: ``extensions.settings`` on current Chrome; ``Preferences`` is read as a fallback for older
#: profiles that kept it there. Measured, not assumed: on Chrome 152 the plain ``Preferences``
#: carries an empty ``extensions.settings`` while the real list sits in the secure file.
_EXTENSION_PREFERENCE_FILES = ("Secure Preferences", "Preferences")


def find_extension_id(
    preferences: Mapping[str, Any],
    *,
    name: str = "",
    source_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the id Chrome gave an unpacked extension, or None when it is not installed.

    An unpacked extension with no ``key`` in its manifest gets an id DERIVED FROM ITS PATH, so
    the id differs per machine and cannot be hard-coded. It has to be looked up, and the two
    things we know about our own extension are where it was cloned to and what it is called.

    ``source_dir`` is tried first and is the stronger signal: it identifies the copy this
    add-on installed, rather than any build of the extension the user may also have loaded.
    ``name`` is the fallback, for a user who loaded it from somewhere else.

    Args:
        preferences: The decoded contents of a profile's ``Secure Preferences``/``Preferences``.
        name: The extension's manifest name, matched case-insensitively.
        source_dir: The unpacked directory Chrome was pointed at.

    Returns:
        The extension id, or None when no entry matches.
    """
    extensions = preferences.get("extensions")
    settings = extensions.get("settings") if isinstance(extensions, Mapping) else None
    if not isinstance(settings, Mapping):
        return None

    wanted_path = (
        str(source_dir).replace("\\", "/").rstrip("/").lower() if source_dir else ""
    )
    wanted_name = name.strip().lower()
    by_name: Optional[str] = None

    for extension_id, entry in settings.items():
        if not isinstance(entry, Mapping):
            continue
        entry_path = str(entry.get("path") or "").replace("\\", "/").rstrip("/").lower()
        if wanted_path and entry_path == wanted_path:
            return str(extension_id)
        manifest = entry.get("manifest")
        entry_name = (
            str(manifest.get("name") or "") if isinstance(manifest, Mapping) else ""
        )
        if (
            wanted_name
            and entry_name.strip().lower() == wanted_name
            and by_name is None
        ):
            by_name = str(extension_id)
    return by_name


def read_profile_preferences(
    user_data_dir: Optional[Path], profile_directory: str
) -> dict[str, Any]:
    """Read a profile's extension preferences, trying the secure file first.

    Never raises, for the same reason :func:`read_local_state` does not: a locked or missing
    file means "cannot tell", which the caller reports as a plain message rather than a crash.
    """
    if user_data_dir is None or not profile_directory:
        return {}
    for filename in _EXTENSION_PREFERENCE_FILES:
        try:
            text = (user_data_dir / profile_directory / filename).read_text(
                encoding="utf-8", errors="replace"
            )
            data = json.loads(text)
        except (OSError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        extensions = data.get("extensions")
        if isinstance(extensions, Mapping) and extensions.get("settings"):
            return dict(data)
    return {}


def installed_extension_id(
    *,
    name: str = "",
    source_dir: Optional[Path] = None,
    profile: Optional[ChromeProfile] = None,
    platform: str = "",
) -> Optional[str]:
    """The id of our unpacked extension in ``profile`` (default: the last-used one), or None."""
    user_data_dir = chrome_user_data_dir(platform)
    profile = profile or preferred_profile(platform)
    if profile is None:
        return None
    preferences = read_profile_preferences(user_data_dir, profile.directory)
    return find_extension_id(preferences, name=name, source_dir=source_dir)
