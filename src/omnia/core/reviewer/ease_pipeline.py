"""The reviewer ease pipeline (ADR-003).

``Reviewer._answerCard`` is wrapped **exactly once**. Features that change the graded ease
register an ordered *ease transformer* ``(card, ease) -> ease | None`` (return None to leave
the ease unchanged). When a card is answered, the requested ease is folded through every
registered transformer in ascending ``priority`` order, so features compose deterministically
instead of fighting over the monkeypatch.

The folding logic (:func:`fold_ease`) is pure and unit-tested; the :class:`EasePipeline`
class is the thin Anki glue that owns the single patch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

# An ease transformer: given the card and the ease decided so far, return a new ease
# (1=Again..4=Easy) or None to pass the current ease through unchanged.
EaseTransformer = Callable[[Any, int], Optional[int]]

EASE_MIN = 1
EASE_MAX = 4


@dataclass(order=True)
class _Entry:
    priority: int
    plugin_id: str
    # Sorting compares (priority, plugin_id) only; never the callable (functions aren't
    # orderable, and plugin_id is unique anyway — this just makes the tie-break explicit).
    fn: EaseTransformer = field(compare=False)


def fold_ease(card: Any, requested: int, entries: list[_Entry]) -> int:
    """Fold ``requested`` through ``entries`` (already priority-sorted) and return the ease.

    A transformer returning None leaves the running ease unchanged. Results are clamped to
    the valid 1..4 range so a misbehaving transformer can't produce an invalid ease.
    """
    ease = requested
    for entry in entries:
        result = entry.fn(card, ease)
        if result is not None:
            ease = max(EASE_MIN, min(EASE_MAX, int(result)))
    return ease


class EasePipeline:
    """Owns the single ``Reviewer._answerCard`` wrap and the ordered transformer set."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._installed = False
        self._orig: Optional[Callable[..., Any]] = None

    # --- registration (used by feature plugins via the context) --------------------
    def add_transformer(
        self, plugin_id: str, fn: EaseTransformer, priority: int = 100
    ) -> None:
        """Register (or replace) ``plugin_id``'s ease transformer at ``priority``."""
        self._entries[plugin_id] = _Entry(priority, plugin_id, fn)

    def remove_transformer(self, plugin_id: str) -> None:
        """Remove ``plugin_id``'s transformer if present (safe if absent)."""
        self._entries.pop(plugin_id, None)

    def has_transformers(self) -> bool:
        """Return True if any transformer is currently registered."""
        return bool(self._entries)

    # --- computation ----------------------------------------------------------------
    def compute_ease(self, card: Any, requested: int) -> int:
        """Return the ease ``card`` should be graded at, folding all transformers."""
        return fold_ease(card, requested, sorted(self._entries.values()))

    # --- Anki glue ------------------------------------------------------------------
    def install(self) -> None:
        """Wrap ``Reviewer._answerCard`` exactly once. Idempotent."""
        if self._installed:
            return
        from aqt.reviewer import Reviewer

        orig = Reviewer._answerCard
        self._orig = orig
        pipeline = self

        # Name mirrors Anki's Reviewer._answerCard for clarity at the patch site.
        def _answerCard(reviewer: Any, ease: int) -> Any:  # noqa: N802
            card = getattr(reviewer, "card", None)
            if card is not None and pipeline._entries:
                ease = pipeline.compute_ease(card, ease)
            return orig(reviewer, ease)

        Reviewer._answerCard = _answerCard  # type: ignore[method-assign]
        self._installed = True

    def uninstall(self) -> None:
        """Restore the original ``_answerCard`` (used on add-on teardown)."""
        if not self._installed or self._orig is None:
            return
        from aqt.reviewer import Reviewer

        Reviewer._answerCard = self._orig  # type: ignore[method-assign]
        self._installed = False
        self._orig = None
