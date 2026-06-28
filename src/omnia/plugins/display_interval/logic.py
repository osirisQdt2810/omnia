"""Pure interval-formatting logic (no Anki imports — unit-testable).

Mirrors the reference ``_fmt_ivl``'s compact units: ``<Nm`` for sub-10-minute, ``Nm`` for
10+ minutes, ``Nh`` for hours, ``Nd`` for days — extended (beyond the reference, which stops
at days) with ``Nmo`` / ``N.Ny`` for the long intervals Anki itself shows.
"""

from __future__ import annotations

_MINUTE = 60
_HOUR = 3600
_DAY = 86_400
_MONTH = _DAY * 30
_YEAR = _DAY * 365


def format_interval(seconds: int) -> str:
    """Format an interval in seconds as a compact human string (e.g. ``"<5m"``, ``"3d"``)."""
    if seconds <= 0:
        return "0"
    if seconds < _HOUR:
        minutes = max(1, round(seconds / _MINUTE))
        return f"<{minutes}m" if minutes < 10 else f"{minutes}m"
    if seconds < _DAY:
        return f"{max(1, round(seconds / _HOUR))}h"
    if seconds < _MONTH:
        return f"{max(1, round(seconds / _DAY))}d"
    if seconds < _YEAR:
        return f"{round(seconds / _MONTH)}mo"
    return f"{seconds / _YEAR:.1f}y"
