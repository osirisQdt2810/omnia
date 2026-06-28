"""Typing Accuracy feature: grade typed cards from typing accuracy.

On the answer side, injected JS measures the typed-answer markup and reports the accuracy
ratio over the ``pycmd`` bridge. Python maps it to an ease and stages it; when the card is
answered, an ease transformer substitutes that ease (cooperating with overdue_guard via the
shared ease pipeline). Cards without a typed answer are untouched.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import ConfigField, FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.features.typed_accuracy.logic import decide_ease
from omnia.features.typed_accuracy.stats import (
    StatsStore,
    stats_card_html,
    summarize,
)

_PRIORITY = 100  # before overdue_guard (200), which may then cap this grade

# Measures Anki's typed-answer comparison and reports the accuracy ratio. Sends nothing when
# there is no typed answer (total length 0), so non-typed cards are unaffected.
_ANSWER_JS = """
(function () {
  var sum = function (cls) {
    var n = 0;
    document.querySelectorAll(cls).forEach(function (e) { n += e.textContent.length; });
    return n;
  };
  var good = sum('.typeGood'), bad = sum('.typeBad'), missed = sum('.typeMissed');
  var total = good + bad + missed;
  if (total > 0) {
    pycmd('omnia:' + JSON.stringify({
      plugin: 'typed_accuracy', op: 'rated', data: { ratio: good / total }
    }));
  }
})();
""".strip()


@register("typed_accuracy")
class TypedAccuracyPlugin(FeaturePlugin):
    """Auto-grades typed cards (again/hard/good/easy) from typing accuracy."""

    name = "Typing Accuracy"
    description = (
        "Grade typed cards again/hard/good/easy from how accurately you typed."
    )
    order = 20

    def __init__(self) -> None:
        self._pending: dict[int, int] = {}
        self._store: Optional[StatsStore] = None
        self._show_stats = True

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._show_stats = settings.show_stats
        self._store = StatsStore(ctx.paths.user_files_dir / "typed_accuracy_stats.json")

        def handler(data: dict[str, Any], _context: Any) -> None:
            cid = self._current_cid()
            if cid is not None:
                ratio = float(data.get("ratio", 0.0))
                self._pending[cid] = decide_ease(
                    ratio, settings.threshold, settings.pass_ease
                )
                if self._store is not None:
                    self._store.record(
                        ratio,
                        settings.threshold,
                        deck_id=self._current_card_did(),
                        now=time.time(),
                    )

        def transform(card: Any, _ease: int) -> Optional[int]:
            return self._pending.pop(getattr(card, "id", None), None)

        ctx.web.add_asset(self.id, WebAsset(answer_js=_ANSWER_JS))
        ctx.web.add_handler(self.id, "rated", handler)
        ctx.ease.add_transformer(self.id, transform, priority=_PRIORITY)
        # Clear any stale pending ease when a card's question is shown, so a score from a
        # prior (abandoned) review can never leak onto a later answer of the same card.
        anki_compat.subscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.subscribe_hook("overview_will_render_content", self._on_overview)

    def on_disable(self, ctx: PluginContext) -> None:
        ctx.web.remove(self.id)
        ctx.ease.remove_transformer(self.id)
        anki_compat.unsubscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.unsubscribe_hook("overview_will_render_content", self._on_overview)
        self._pending.clear()
        self._store = None

    def _on_question(self, card: Any) -> None:
        self._pending.pop(getattr(card, "id", None), None)

    def _on_overview(self, _overview: Any, content: Any) -> None:
        """Append the accuracy stats card to the deck-overview content (when enabled)."""
        if not self._show_stats or self._store is None:
            return
        deck_id = self._current_deck_id()
        results = self._store.results(deck_id)
        if not results and deck_id is not None:
            results = self._store.results()  # fall back to all-decks so it isn't blank
        content.table += stats_card_html(summarize(results))

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                "threshold",
                "Pass threshold (accuracy 0–1)",
                "float",
                0.7,
                minimum=0.0,
                maximum=1.0,
            ),
            ConfigField(
                "pass_ease",
                "Ease on a pass",
                "choice",
                "good",
                choices=("good", "easy"),
            ),
            ConfigField(
                "show_stats",
                "Show accuracy stats on the deck overview",
                "bool",
                True,
            ),
        ]

    @staticmethod
    def _current_cid() -> Optional[int]:
        reviewer = getattr(anki_compat.main_window(), "reviewer", None)
        card = getattr(reviewer, "card", None)
        return getattr(card, "id", None) if card is not None else None

    @staticmethod
    def _current_card_did() -> Optional[int]:
        """Return the deck id of the card under review, or None."""
        reviewer = getattr(anki_compat.main_window(), "reviewer", None)
        card = getattr(reviewer, "card", None)
        return getattr(card, "did", None) if card is not None else None

    @staticmethod
    def _current_deck_id() -> Optional[int]:
        """Return the currently selected deck id (None if unavailable across versions)."""
        col = getattr(anki_compat.main_window(), "col", None)
        decks = getattr(col, "decks", None) if col is not None else None
        getter = getattr(decks, "get_current_id", None) if decks is not None else None
        if not callable(getter):
            return None
        try:
            return int(getter())
        except (TypeError, ValueError):
            return None
