"""Pure interval-formatting logic (no Anki imports — unit-testable)."""

from __future__ import annotations

_MINUTE = 60
_HOUR = 3600
_DAY = 86_400
_MONTH = _DAY * 30
_YEAR = _DAY * 365


def format_interval(seconds: int) -> str:
    """Format an interval in seconds as a compact human string (e.g. ``"3d"``, ``"2mo"``)."""
    if seconds < _MINUTE:
        return "<1m"
    if seconds < _HOUR:
        return f"{round(seconds / _MINUTE)}m"
    if seconds < _DAY:
        return f"{round(seconds / _HOUR)}h"
    if seconds < _MONTH:
        return f"{round(seconds / _DAY)}d"
    if seconds < _YEAR:
        return f"{round(seconds / _MONTH)}mo"
    return f"{seconds / _YEAR:.1f}y"
