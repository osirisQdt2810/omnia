"""Typing Accuracy feature: grade typed cards from typing accuracy + an interactive stats panel.

On the answer side, injected JS measures Anki's typed-answer markup (``.typeGood`` /
``.typeBad`` / ``.typeMissed``), polling briefly for late-rendered markup, then ALWAYS reports
a result — including the empty / no-markup case, which forces Hard. Python maps the accuracy
ratio to an ease and stages it; when the card is answered, an ease transformer substitutes
that ease (cooperating with overdue_guard via the shared ease pipeline at priority 100/200).
Every outcome is also logged (good/bad/miss/empty) to a SQLite table in the collection, which
an interactive donut panel on the Statistics screen queries.

Single-application note: the cooperative ease pipeline is the one place an answer's ease is
rewritten. Pressing any review button (Enter included — Anki maps it to a button) routes
through the wrapped ``_answerCard``, where the staged ease is substituted; ``auto_answer="no"``
stages nothing so the user's own press stands. This already reproduces the reference's
rated-answer Enter behaviour, so we intentionally do NOT add a separate Enter interceptor /
``answer_ease`` op: that would risk grading the card twice. See the audit notes in the plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import ConfigField, FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.features.typed_accuracy.logic import decide_ease, result_code
from omnia.features.typed_accuracy.stats_injector import StatsInjector
from omnia.features.typed_accuracy.store import SessionTracker, TypedAnswerLog

_PRIORITY = 100  # before overdue_guard (200), which may then cap this grade

# The panel's web assets ship next to this module (read directly, not via web-export).
_WEB_DIR = Path(__file__).resolve().parent / "web"

# Measures Anki's typed-answer comparison on the answer side and reports BOTH the accuracy
# ratio and a 4-way result code. It polls up to 40 times at ~50ms for late-rendered markup,
# then reports anyway: a type-answer card with no markup is an EMPTY answer (ratio 0 -> Hard).
# Non-type-answer cards (no #typeans) report nothing, so they are unaffected.
_ANSWER_JS = """
(function () {
  function send(op, data) {
    try {
      pycmd("omnia:" + JSON.stringify({ plugin: "typed_accuracy", op: op, data: data }));
    } catch (e) {}
  }
  function textLen(root, selector) {
    var n = 0, els = root.querySelectorAll(selector);
    for (var i = 0; i < els.length; i++) n += (els[i].textContent || "").length;
    return n;
  }
  var tries = 0;
  function run() {
    tries++;
    var el = document.getElementById("typeans");
    if (!el) return;  // not a type-answer card

    var hasGood = el.querySelector(".typeGood") != null;
    var hasBad = el.querySelector(".typeBad") != null;
    var hasMiss = el.querySelector(".typeMissed") != null;
    var hadMarkup = hasGood || hasBad || hasMiss;

    if (hadMarkup) {
      var goodLen = textLen(el, ".typeGood");
      var badLen = textLen(el, ".typeBad");
      var missLen = textLen(el, ".typeMissed");
      var denom = goodLen + badLen + missLen;
      var ratio = denom ? goodLen / denom : 0.0;
      send("rated", { ratio: ratio, hasGood: hasGood, hasBad: hasBad, hasMiss: hasMiss });
      return;
    }

    if (tries < 40) { setTimeout(run, 50); return; }

    // No markup after polling: an empty typed answer. ratio 0 forces Hard.
    send("rated", { ratio: 0.0, hasGood: false, hasBad: false, hasMiss: false });
  }
  run();
})();
""".strip()


@register("typed_accuracy")
class TypedAccuracyPlugin(FeaturePlugin):
    """Auto-grades typed cards (again/hard/good/easy) and shows an interactive stats panel."""

    name = "Typing Accuracy"
    description = (
        "Grade typed cards again/hard/good/easy from how accurately you typed, "
        "and show an interactive accuracy panel on the Statistics screen."
    )
    group = "Grading"
    tooltip = (
        "Sets the grade from how accurately you typed the answer. "
        "Cooperates with Overdue Guard: Typing Accuracy decides the grade first, "
        "then Overdue Guard may cap it for very overdue cards. Both can be on at once — "
        "they do not conflict and need not be mutually exclusive."
    )
    order = 20

    def __init__(self) -> None:
        self._pending: dict[int, int] = {}
        self._sessions = SessionTracker()
        self._injector: Optional[StatsInjector] = None
        self._threshold = 0.7
        self._pass_ease = "good"

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._threshold = settings.threshold
        self._pass_ease = settings.pass_ease
        self._injector = StatsInjector(_WEB_DIR, ctx.log)

        ctx.web.add_asset(self.id, WebAsset(answer_js=_ANSWER_JS))
        ctx.web.add_handler(self.id, "rated", self._on_rated)
        ctx.web.add_handler(self.id, "query", self._on_query)
        ctx.web.add_handler(self.id, "get_current_did", self._on_get_current_did)
        ctx.web.add_handler(
            self.id, "get_session_open_ms", self._on_get_session_open_ms
        )
        ctx.web.add_handler(self.id, "dbg", self._on_dbg)
        ctx.ease.add_transformer(self.id, self._transform, priority=_PRIORITY)

        # Clear any stale pending ease when a card's question is shown, so a score from a
        # prior (abandoned) review can never leak onto a later answer of the same card.
        anki_compat.subscribe_hook("reviewer_did_show_question", self._on_question)
        # Record the session-open time per deck when review begins (for the "current" range).
        anki_compat.subscribe_hook("state_did_change", self._on_state_change)
        # Mount the interactive panel onto the Statistics screen.
        anki_compat.subscribe_hook(
            "webview_did_inject_style_into_page", self._on_inject_style
        )

    def on_disable(self, ctx: PluginContext) -> None:
        ctx.web.remove(self.id)
        ctx.ease.remove_transformer(self.id)
        anki_compat.unsubscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.unsubscribe_hook("state_did_change", self._on_state_change)
        anki_compat.unsubscribe_hook(
            "webview_did_inject_style_into_page", self._on_inject_style
        )
        self._pending.clear()
        self._sessions.clear()
        self._injector = None

    # --- ease pipeline --------------------------------------------------------------
    def _transform(self, card: Any, _ease: int) -> Optional[int]:
        # pop is idempotent: a second answer path for the same card finds nothing staged,
        # so the staged ease is applied exactly once (the pipeline is the only apply point).
        return self._pending.pop(getattr(card, "id", None), None)

    # --- pycmd handlers (data, context) -> result ----------------------------------
    def _on_rated(self, data: dict[str, Any], _context: Any) -> dict[str, Any]:
        """Stage the decided ease for the current card and log the 4-way result."""
        cid = self._current_cid()
        if cid is None:
            return {"ok": False, "error": "no current card"}

        ratio = float(data.get("ratio", 0.0))
        ease = decide_ease(ratio, self._threshold, self._pass_ease)
        if ease is not None:
            self._pending[cid] = ease
        else:
            # auto_answer == "no": stage nothing so the user's own press stands.
            self._pending.pop(cid, None)

        result = result_code(
            bool(data.get("hasGood")),
            bool(data.get("hasBad")),
            bool(data.get("hasMiss")),
        )
        self._insert_log(cid, result)
        return {"ok": True}

    def _on_query(self, data: dict[str, Any], _context: Any) -> dict[str, Any]:
        """Aggregate stats for a deck/time-window (drives the panel donut)."""
        store = self._log_store()
        if store is None:
            return {"ok": False, "error": "collection unavailable"}
        try:
            did = int(data["did"])
            include_subdecks = bool(data.get("includeSubdecks", False))
            start_ms = int(data["startMs"])
            end_ms = int(data["endMs"])
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"missing/invalid fields: {exc}"}
        return {
            "ok": True,
            "data": store.query_stats(did, include_subdecks, start_ms, end_ms),
        }

    def _on_get_current_did(
        self, _data: dict[str, Any], _context: Any
    ) -> dict[str, Any]:
        """Return the currently selected deck id (for the panel's default scope)."""
        did = self._current_deck_id()
        if did is None:
            return {"ok": False, "error": "no current deck"}
        return {"ok": True, "did": did}

    def _on_get_session_open_ms(
        self, data: dict[str, Any], _context: Any
    ) -> dict[str, Any]:
        """Return the session-open time for a deck (the 'current' range start)."""
        try:
            did = int(data["did"])
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "openMs": self._sessions.get_open_ms(did)}

    def _on_dbg(self, data: dict[str, Any], _context: Any) -> dict[str, Any]:
        """Forward a JS debug message to the Omnia logger."""
        from omnia.core.logging import get_logger

        get_logger().debug("typed_accuracy[JS] %s", data.get("msg", ""))
        return {"ok": True}

    # --- hooks ----------------------------------------------------------------------
    def _on_question(self, card: Any) -> None:
        self._pending.pop(getattr(card, "id", None), None)

    def _on_state_change(self, new_state: str, old_state: str) -> None:
        """Record the session-open time when entering the review state."""
        if new_state == "review" and old_state != "review":
            did = self._current_deck_id()
            if did is not None:
                self._sessions.mark_review_entered(did)

    def _on_inject_style(self, webview: Any) -> None:
        """Inject the stats panel after Anki finishes styling a page (gets the webview).

        Anki's ``webview_did_inject_style_into_page`` passes the styled ``AnkiWebView``. The
        panel JS gates itself to the stats DOM, so injecting on every styled page is safe;
        the asset eval is cheap and idempotent.
        """
        if self._injector is None:
            return
        if webview is not None and hasattr(webview, "eval"):
            self._injector.inject(webview)

    # --- config ---------------------------------------------------------------------
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
                "Auto-answer on a pass",
                "choice",
                "good",
                choices=("good", "easy", "no"),
                help="'no' stages no ease (your own press stands); a fail always forces Hard.",
            ),
            ConfigField(
                "show_stats",
                "Show the accuracy panel on the Statistics screen",
                "bool",
                True,
            ),
        ]

    # --- helpers --------------------------------------------------------------------
    def _insert_log(self, cid: int, result: int) -> None:
        store = self._log_store()
        if store is None:
            return
        did = self._current_deck_id() or 0
        card_did = self._current_card_did() or did
        store.insert_log(cid, did, card_did, result)

    def _log_store(self) -> Optional[TypedAnswerLog]:
        """Build a :class:`TypedAnswerLog` over the live collection DB, or None if unavailable."""
        col = getattr(anki_compat.main_window(), "col", None)
        db = getattr(col, "db", None) if col is not None else None
        if db is None:
            return None
        decks = getattr(col, "decks", None)
        provider = self._deck_provider(decks) if decks is not None else None
        return TypedAnswerLog(db, deck_provider=provider)

    @staticmethod
    def _deck_provider(decks: Any) -> Any:
        """Return a callable yielding ``(deck_id, deck_name)`` pairs from Anki's decks."""

        def provider() -> list[tuple[int, str]]:
            return [(int(d["id"]), str(d["name"])) for d in decks.all()]

        return provider

    @staticmethod
    def _current_cid() -> Optional[int]:
        reviewer = getattr(anki_compat.main_window(), "reviewer", None)
        card = getattr(reviewer, "card", None)
        return getattr(card, "id", None) if card is not None else None

    @staticmethod
    def _current_card_did() -> Optional[int]:
        """Return the original deck id of the card under review, or None."""
        reviewer = getattr(anki_compat.main_window(), "reviewer", None)
        card = getattr(reviewer, "card", None)
        return getattr(card, "did", None) if card is not None else None

    @staticmethod
    def _current_deck_id() -> Optional[int]:
        """Return the currently selected (study) deck id, or None if unavailable."""
        col = getattr(anki_compat.main_window(), "col", None)
        decks = getattr(col, "decks", None) if col is not None else None
        getter = getattr(decks, "get_current_id", None) if decks is not None else None
        if not callable(getter):
            return None
        try:
            return int(getter())
        except (TypeError, ValueError):
            return None
