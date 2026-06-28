"""Pure auto-flip helpers (no Anki imports — unit-testable)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnia.core.config.models import AutoFlipSettings


def delay_ms(seconds: float) -> int:
    """Convert a (non-negative) delay in seconds to whole milliseconds."""
    return max(0, round(seconds * 1000))


def effective_delays(
    settings: AutoFlipSettings, deck_id: object
) -> tuple[bool, int, int]:
    """Resolve the auto-flip behaviour for one deck.

    Mirrors the reference add-on's two-flag deck gate. A per-deck override (keyed by
    ``str(deck_id)`` in ``settings.per_deck``) decides how the deck behaves:

    * ``use_global=True`` → the deck is on but defers to the *global* delays (the
      reference's ``use_general``); the override's own delays are ignored.
    * ``use_global=False`` + ``enabled=True`` → the deck is on and uses the override's
      delays (the reference's ``use_deck``).
    * ``use_global=False`` + ``enabled=False`` → auto-flip is off for this deck.

    With no override row the global delays apply and the deck is on (the plugin being on
    == enabled). The feature has no global ``enabled`` flag — only per-deck overrides can
    switch a deck off.

    Args:
        settings: The auto-flip settings (global delays + per-deck overrides).
        deck_id: The current card's deck id (coerced to ``str`` for the lookup).

    Returns:
        ``(enabled, question_delay_ms, answer_delay_ms)``. ``enabled=False`` means "skip
        auto-flip for this deck".
    """
    global_delays = (
        delay_ms(settings.delay_question_seconds),
        delay_ms(settings.delay_answer_seconds),
    )
    override = settings.per_deck.get(str(deck_id)) if deck_id is not None else None
    if override is None:
        return (True, *global_delays)
    if override.use_global:
        return (True, *global_delays)
    if not override.enabled:
        return (False, *global_delays)
    return (
        True,
        delay_ms(override.delay_question_seconds),
        delay_ms(override.delay_answer_seconds),
    )


# SRT-style timestamp: HH:MM:SS,mmm or HH:MM:SS.mmm (comma or dot before milliseconds).
_SRT_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}[.,]\d{3}")
# The reference's "myview.mpv" clip player passes the clip bounds as --range=START-END.
_MPV_RANGE_MARKERS = ("myview.mpv", "--range=")


def _srt_to_seconds(timestamp: str) -> float:
    """Convert an ``HH:MM:SS,mmm`` / ``HH:MM:SS.mmm`` timestamp to seconds."""
    hours, minutes, rest = timestamp.split(":", 2)
    seconds, millis = re.split(r"[.,]", rest, maxsplit=1)
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_mpv_range_extra_seconds(av_arg_or_command: str) -> float:
    """Return the total clip duration (seconds) encoded in an mpv ``--range=`` argument.

    The reference's external "myview.mpv" player receives the clip bounds as
    ``--range=HH:MM:SS,mmm-HH:MM:SS,mmm`` (SRT-style timestamps). Anki cannot know the clip
    length ahead of time, so the wait is extended by the clip's ``end - start`` duration to
    keep auto-flip from firing before the clip finishes. Timestamps are read in document
    order and paired (start, end), summing every pair — matching the reference's
    ``get_mpv_view_add_time``.

    A string without both ``myview.mpv`` and ``--range=`` (or without timestamps) yields
    ``0.0``. This is a pure parser; the feature glue supplies the card's audio command.

    Args:
        av_arg_or_command: An mpv command line / av-tag argument string to inspect.

    Returns:
        The summed clip duration in seconds (``0.0`` when no range is present).
    """
    if not all(marker in av_arg_or_command for marker in _MPV_RANGE_MARKERS):
        return 0.0
    stamps = _SRT_TIMESTAMP_RE.findall(av_arg_or_command)
    total = 0.0
    for index in range(0, len(stamps) - 1, 2):
        start = _srt_to_seconds(stamps[index])
        end = _srt_to_seconds(stamps[index + 1])
        total += end - start
    return total
