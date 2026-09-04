"""Finding the Chrome profile a user actually uses, and where its executable lives.

Chrome numbers profile directories in creation order (``Default``, ``Profile 1``, ``Profile
16``…) and shows the user a display name that has nothing to do with it. Someone with eight
profiles opening ``chrome://extensions`` gets whichever one Chrome felt like, which for
installing an extension is the wrong one often enough to matter — so this reads Chrome's own
``Local State`` and picks the profile Chrome itself last used.

Which profile Chrome last used is the right answer for INSTALLING, and the wrong one for
finding something already installed: a user whose everyday profile is "Default" can perfectly
well have loaded the clipper in "Profile 3" months ago, and reporting a running extension as
absent because it is not in the last-used profile is the bug :func:`locate_extension` exists
to close. So installing still aims at the preferred profile, while looking something UP walks
every profile — preferred first, so a machine where both agree behaves exactly as before.

Pure logic, with one exception: the parsing takes the already-decoded ``Local State`` mapping,
so it unit-tests without a browser or a platform. :func:`find_extension_id` is the one function
that touches disk — it reads ``manifest.json`` out of an unpacked extension's directory when
Chrome cached no manifest for it, which for unpacked loads is the common case.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
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
    cache = _info_cache(local_state)
    last_used = profile.get("last_used")
    if isinstance(last_used, str) and last_used:
        return _profile_from_cache(cache, last_used)
    if not cache:
        return None
    newest = max(
        cache,
        key=lambda directory: float(
            (cache.get(directory) or {}).get("active_time", 0) or 0
        ),
    )
    return _profile_from_cache(cache, newest)


def _info_cache(local_state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Chrome's per-profile metadata, keyed by profile directory; ``{}`` when absent."""
    profile = local_state.get("profile")
    if not isinstance(profile, Mapping):
        return {}
    cache = profile.get("info_cache")
    return cache if isinstance(cache, Mapping) else {}


def _profile_from_cache(cache: Mapping[str, Any], directory: str) -> ChromeProfile:
    """Describe the profile in ``directory``, falling back to the directory as its name.

    Chrome can name a ``last_used`` directory that ``info_cache`` says nothing about, so a
    missing entry is normal and must still produce a usable profile rather than nothing.
    """
    meta = cache.get(directory)
    meta = meta if isinstance(meta, Mapping) else {}
    return ChromeProfile(
        directory=directory,
        name=str(meta.get("name", "") or directory),
        last_active=float(meta.get("active_time", 0) or 0),
    )


def profile_search_order(local_state: Mapping[str, Any]) -> list[ChromeProfile]:
    """Every profile in ``local_state``, in the order a lookup should try them.

    The preferred profile leads, so a lookup that would have succeeded before still resolves
    to the same profile and nothing about the common case changes; the rest follow
    most-recently-active first, because between two profiles that both hold the extension the
    one the user actually browses in is the one they meant.
    """
    preferred = pick_profile(local_state)
    cache = _info_cache(local_state)
    others = [
        _profile_from_cache(cache, directory)
        for directory in cache
        if preferred is None or directory != preferred.directory
    ]
    # Directory as the tie-break: two profiles with the same (often zero) active_time must not
    # reorder between calls, or the same click reloads a different profile each time.
    others.sort(key=lambda profile: (-profile.last_active, profile.directory))
    return ([preferred] if preferred is not None else []) + others


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


def chrome_profiles(platform: str = "") -> list[ChromeProfile]:
    """Every Chrome profile on this machine, preferred first; empty when Chrome never ran."""
    return profile_search_order(read_local_state(chrome_user_data_dir(platform)))


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


def _entry_name(entry: Mapping[str, Any]) -> str:
    """The extension's name for ``entry``, from Chrome's copy or from disk.

    Chrome usually caches the manifest under ``"manifest"``, but for an extension loaded
    unpacked it frequently records only the PATH -- a real profile here had every key from
    ``active_permissions`` to ``service_worker_registration_info`` and no ``manifest`` at all.
    Reading the name off that entry alone therefore yields "", the name fallback never fires,
    and a running extension is reported as not loaded. So when Chrome kept no manifest, this
    reads ``manifest.json`` out of the directory Chrome DID record.

    Returns:
        The manifest name, or ``""`` when neither source has one.
    """
    manifest = entry.get("manifest")
    if isinstance(manifest, Mapping):
        cached = str(manifest.get("name") or "").strip()
        if cached:
            return cached
    # The RAW path, never the lowercased one the matcher compares with: a lowercased path
    # does not open on a case-sensitive filesystem, and that silence would read as "not
    # loaded". Chrome stores a RELATIVE path for store extensions ("<id>/<version>"); only an
    # absolute path is an unpacked load there is a directory to read from.
    raw = str(entry.get("path") or "")
    if not raw or not Path(raw).is_absolute():
        return ""
    try:
        on_disk = json.loads((Path(raw) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(on_disk.get("name") or "").strip() if isinstance(on_disk, dict) else ""


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
        entry_name = _entry_name(entry) if wanted_name and by_name is None else ""
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


@dataclass(frozen=True)
class ExtensionLocation:
    """Where an extension was found: the profile holding it, and the id Chrome gave it there.

    The two travel together because neither is usable alone — the id addresses the extension's
    own pages, and only the profile that recorded that id can open them.
    """

    profile: ChromeProfile
    extension_id: str


def locate_extension(
    profiles: Sequence[ChromeProfile],
    *,
    name: str = "",
    source_dir: Optional[Path] = None,
    platform: str = "",
) -> Optional[ExtensionLocation]:
    """Find our extension across ``profiles``: the copy we installed first, any copy second.

    Chrome gives an unpacked extension a different id in every profile it is loaded into, so
    "is it installed?" can only be answered per profile. The priority rule is a property of the
    LOOKUP, not of a profile: a path match (the clone this add-on installed) in a later profile
    must beat a name match (some other build of the extension) in an earlier one — otherwise a
    user who also keeps a dev checkout loaded in their everyday profile upgrades the clipper,
    presses Reload, and the copy that was just upgraded is never reloaded. So the search is two
    passes over the same order: exact path everywhere, then name everywhere. Profile order only
    breaks ties within a pass. Each pass costs one preferences read per profile, ~2 ms total.

    ``profiles`` is a ``Sequence`` because the caller enumerates it again to name what was
    searched when nothing is found; a generator would leave that list empty.

    Args:
        profiles: Profiles to search, in priority order (see :func:`profile_search_order`).
        name: The extension's manifest name, used when no profile has a path match.
        source_dir: The unpacked directory this add-on cloned the extension into.
        platform: ``sys.platform`` override (for tests).
    """
    if source_dir is not None:
        for profile in profiles:
            exact = installed_extension_id(
                source_dir=source_dir, profile=profile, platform=platform
            )
            if exact:
                return ExtensionLocation(profile=profile, extension_id=exact)
    if name:
        for profile in profiles:
            named = installed_extension_id(
                name=name, profile=profile, platform=platform
            )
            if named:
                return ExtensionLocation(profile=profile, extension_id=named)
    return None
