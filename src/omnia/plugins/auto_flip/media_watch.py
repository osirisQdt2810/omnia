"""Loader for the auto-flip HTML5 media watcher JS (``gui/auto_flip/web/media_watch.js``).

Card templates that play audio through their own JS on an HTML5 ``<audio>``/``<video>``
element (instead of Anki ``[sound:...]`` AV tags) are invisible to ``av_player`` — so the
wait-for-audio arming would fire mid-playback. The watcher JS reports ``media_busy`` /
``media_idle`` over the ``pycmd`` bridge (routed by the web injector as
``omnia:auto_flip:<op>``); the plugin holds / re-arms its countdown off those events.

Kept as a tiny loader module (mirroring ``countdown.py``) so the JS lives with the other
web assets and the plugin stays import-light.
"""

from __future__ import annotations

import omnia.gui.auto_flip as _autoflip_gui
from omnia.gui.assets import read_asset


def build_media_watch_js() -> str:
    """Return the media-watcher JS to inject on both reviewer sides (idempotent script)."""
    return read_asset(_autoflip_gui.__file__, "web", "media_watch.js").strip()
