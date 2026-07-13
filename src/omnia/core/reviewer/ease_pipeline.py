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

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

# An ease transformer: ``(card, ease) -> int | None`` (return None to pass the current ease
# through unchanged). A transformer may optionally declare an ``apply`` keyword to distinguish
# the real grade (``apply=True``) from a non-destructive preview (``apply=False``, used by
# display_interval's label); the pipeline threads ``apply`` only to transformers that declare
# it, so a preview never consumes a transformer's staged state.
EaseTransformer = Callable[..., Optional[int]]

EASE_MIN = 1
EASE_MAX = 4


def _accepts_apply(fn: EaseTransformer) -> bool:
    """Return True if ``fn`` declares an ``apply`` parameter (opts into preview semantics)."""
    try:
        return "apply" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


@dataclass(order=True)
class _Entry:
    priority: int
    plugin_id: str
    # Sorting compares (priority, plugin_id) only; never the callable (functions aren't
    # orderable, and plugin_id is unique anyway — this just makes the tie-break explicit).
    fn: EaseTransformer = field(compare=False)
    # True when ``fn`` declares an ``apply`` keyword and so opts into preview semantics (peek
    # instead of consume when apply=False). Detected once at registration, not per fold.
    wants_apply: bool = field(default=False, compare=False)


def fold_ease(
    card: Any,
    requested: int,
    entries: list[_Entry],
    *,
    apply: bool = True,
    ease_max: int = EASE_MAX,
) -> int:
    """Fold ``requested`` through ``entries`` (already priority-sorted) and return the ease.

    A transformer returning None leaves the running ease unchanged. Results are clamped to
    the valid ``1..ease_max`` range so a misbehaving transformer can't produce an invalid ease
    (``ease_max`` reflects the card's actual answer-button count when the caller knows it — a
    learning/relearning card shows only 3 buttons).

    ``apply`` distinguishes the real grade (``True``) from a non-destructive preview
    (``False``); it is threaded only to transformers that declare an ``apply`` parameter, so a
    preview never consumes a transformer's staged state.
    """
    ease = requested
    for entry in entries:
        if entry.wants_apply:
            result = entry.fn(card, ease, apply=apply)
        else:
            result = entry.fn(card, ease)
        if result is not None:
            ease = max(EASE_MIN, min(ease_max, int(result)))
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
        self._entries[plugin_id] = _Entry(priority, plugin_id, fn, _accepts_apply(fn))

    def remove_transformer(self, plugin_id: str) -> None:
        """Remove ``plugin_id``'s transformer if present (safe if absent)."""
        self._entries.pop(plugin_id, None)

    def has_transformers(self) -> bool:
        """Return True if any transformer is currently registered."""
        return bool(self._entries)

    # --- computation ----------------------------------------------------------------
    def compute_ease(
        self,
        card: Any,
        requested: int,
        *,
        apply: bool = True,
        ease_max: int = EASE_MAX,
    ) -> int:
        """Return the ease ``card`` should be graded at, folding all transformers.

        Pass ``apply=False`` for a non-destructive PREVIEW (transformers peek instead of
        consume — used by display_interval's label); ``ease_max`` clamps to the card's real
        answer-button count when the caller knows it.
        """
        return fold_ease(
            card,
            requested,
            sorted(self._entries.values()),
            apply=apply,
            ease_max=ease_max,
        )

    @staticmethod
    def _answer_button_count(reviewer: Any, card: Any) -> int:
        """Return ``card``'s answer-button count (3 for (re)learning, 4 for review cards).

        Falls back to :data:`EASE_MAX` when Anki's scheduler is unreachable (e.g. in tests).
        """
        try:
            return int(reviewer.mw.col.sched.answerButtons(card))
        except (AttributeError, TypeError, ValueError):
            return EASE_MAX

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
                # Resilience: a buggy transformer must NOT break grading — log and fall back
                # to the user's requested ease, then always call the original answer flow.
                # Clamp to the card's real answer-button count (3 for (re)learning cards).
                try:
                    ease = pipeline.compute_ease(
                        card,
                        ease,
                        ease_max=pipeline._answer_button_count(reviewer, card),
                    )
                except Exception:
                    from omnia.core.logging import get_logger

                    get_logger().exception("ease pipeline: compute_ease failed")
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
