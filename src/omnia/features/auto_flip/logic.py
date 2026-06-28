"""Pure auto-flip helpers (no Anki imports — unit-testable)."""

from __future__ import annotations


def delay_ms(seconds: float) -> int:
    """Convert a (non-negative) delay in seconds to whole milliseconds."""
    return max(0, round(seconds * 1000))
