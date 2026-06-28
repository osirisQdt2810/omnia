"""Pure auto-flip helpers (no Anki imports — unit-testable)."""

from __future__ import annotations

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

    A per-deck override (keyed by ``str(deck_id)`` in ``settings.per_deck``) wins over the
    global delays; otherwise the global delays apply and the deck is enabled (the plugin
    being on == enabled). The feature has no global ``enabled`` flag — only per-deck
    overrides can switch a deck off.

    Args:
        settings: The auto-flip settings (global delays + per-deck overrides).
        deck_id: The current card's deck id (coerced to ``str`` for the lookup).

    Returns:
        ``(enabled, question_delay_ms, answer_delay_ms)``. ``enabled=False`` means "skip
        auto-flip for this deck".
    """
    override = settings.per_deck.get(str(deck_id)) if deck_id is not None else None
    if override is not None:
        return (
            override.enabled,
            delay_ms(override.delay_question_seconds),
            delay_ms(override.delay_answer_seconds),
        )
    return (
        True,
        delay_ms(settings.delay_question_seconds),
        delay_ms(settings.delay_answer_seconds),
    )
