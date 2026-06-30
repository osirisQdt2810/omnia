"""Overdue Guard feature: force very overdue cards to Hard/Again via an ease transformer."""

from __future__ import annotations

import time
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.overdue_guard.config import OverdueGuardSettings
from omnia.plugins.overdue_guard.logic import OverdueRule

# Runs after typed_accuracy (priority 100) so it can cap an over-generous typed grade.
_PRIORITY = 200
_MS_PER_DAY = 86_400_000
_SECS_PER_DAY = 86_400


@register("overdue_guard")
class OverdueGuardPlugin(FeaturePlugin):
    """Forces overdue cards to Hard/Again regardless of the pressed/decided ease."""

    name = "Overdue Guard"
    description = (
        "Force very overdue cards to Hard/Again regardless of the pressed button."
    )
    group = "Grading"
    tooltip = (
        "Caps the grade for very overdue cards (forces Hard/Again). "
        "Cooperates with Typing Accuracy: Typing Accuracy sets the grade first, "
        "then Overdue Guard caps it when a card is overdue. Both can be on together — "
        "they run in order through the shared ease pipeline, not against each other. "
        "How “overdue” is measured: a card’s interval (ivl) is the gap Anki scheduled "
        "between reviews, and days-late is how far past the due date you actually review it "
        "(reviewing on time = 0 days late). A card is overdue when days-late ÷ interval ≥ "
        "Ratio, once it is at least Min days late. Example: interval = 10 days and you review "
        "it 8 days after it came due → 8 ÷ 10 = 0.8, so at the default Ratio 0.8 the card is "
        "overdue and capped to Hard. The same card reviewed on time → 0 ÷ 10 = 0 → never overdue."
    )
    order = 40
    config_model = OverdueGuardSettings

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        rule = OverdueRule(
            ratio=settings.ratio,
            min_days=settings.min_days,
            force_again_after_days=settings.force_again_after_days,
        )

        def transform(card: Any, ease: int) -> Optional[int]:
            return self._forced_ease(rule, card, ease)

        ctx.ease.add_transformer(self.id, transform, priority=_PRIORITY)

    def on_disable(self, ctx: PluginContext) -> None:
        ctx.ease.remove_transformer(self.id)

    @staticmethod
    def _forced_ease(rule: OverdueRule, card: Any, ease: int) -> Optional[int]:
        """Gather card timing from Anki and ask the pure rule for the forced ease."""
        last_review_ms = anki_compat.card_last_review_ms(card)
        if last_review_ms is None:
            return None
        ivl_days = float(getattr(card, "ivl", 0))
        # late_days is days PAST DUE = elapsed-since-last-review minus the scheduled interval.
        # (Reviewing on time => ~0.) The rule's ratio/min_days are defined against days-past-due,
        # matching the config tooltip ("0.8 ≈ 80% past due") and OverdueRule's docstring — not
        # raw elapsed days, which would flag every on-time mature card.
        elapsed_days = (time.time() * 1000 - last_review_ms) / _MS_PER_DAY
        late_days = elapsed_days - ivl_days
        hard_secs = anki_compat.next_interval_seconds(card, 2) or 0
        return rule.forced_ease(ease, ivl_days, late_days, hard_secs / _SECS_PER_DAY)
