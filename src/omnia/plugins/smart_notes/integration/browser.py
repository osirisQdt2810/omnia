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
