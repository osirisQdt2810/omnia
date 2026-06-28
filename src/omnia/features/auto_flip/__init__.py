"""Auto Flip feature: auto-advance question -> answer -> grade after a delay.

Schedules a one-shot timer on each reviewer side; flipping the question shows the answer,
and the answer side auto-grades Good (routed through the ease pipeline, so it cooperates
with typed_accuracy / overdue_guard). A manual answer cancels the pending timer, and — when
``wait_for_audio`` is set — the delay restarts when card audio finishes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import ConfigField, FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.features.auto_flip.countdown import build_countdown_js, clear_countdown_js
from omnia.features.auto_flip.logic import effective_delays

_EASE_GOOD = 3
_DECK_MENU_HOOK = "deck_browser_will_show_options_menu"


@register("auto_flip")
class AutoFlipPlugin(FeaturePlugin):
    """Auto-advances the reviewer after configurable delays."""

    name = "Auto Flip"
    description = "Auto-advance question -> answer -> grade after a configurable delay."
    order = 10

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._timer: Optional[Any] = None
        self._action: Optional[Callable[[], None]] = None
        self._delay: int = 0
        self._subs: list[tuple[str, Callable[..., Any]]] = []
        self._wait_for_audio = False
        self._show_timer = True

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._ctx = ctx
        self._wait_for_audio = settings.wait_for_audio
        self._show_timer = settings.show_timer
        self._subscribe("reviewer_did_show_question", self._on_question)
        self._subscribe("reviewer_did_show_answer", self._on_answer)
        self._subscribe("reviewer_will_answer_card", self._on_will_answer)
        self._subscribe("av_player_did_end_playing", self._on_audio_end)
        self._subscribe(_DECK_MENU_HOOK, self._on_deck_menu)

    def on_disable(self, ctx: PluginContext) -> None:
        self._cancel()
        for hook_name, callback in self._subs:
            anki_compat.unsubscribe_hook(hook_name, callback)
        self._subs.clear()
        self._ctx = None

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                "delay_question_seconds",
                "Delay before flipping to answer (s)",
                "float",
                3.0,
                minimum=0,
                maximum=120,
            ),
            ConfigField(
                "delay_answer_seconds",
                "Delay before auto-grading (s)",
                "float",
                3.0,
                minimum=0,
                maximum=120,
            ),
            ConfigField(
                "wait_for_audio",
                "Start the delay only after audio finishes",
                "bool",
                True,
            ),
            ConfigField("show_timer", "Show a countdown in the reviewer", "bool", True),
        ]

    # --- scheduling -----------------------------------------------------------------
    def _subscribe(self, hook_name: str, callback: Callable[..., Any]) -> None:
        anki_compat.subscribe_hook(hook_name, callback)
        self._subs.append((hook_name, callback))

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._action = None
        # No-op when not reviewing; tears down any visible countdown ring.
        anki_compat.reviewer_eval(clear_countdown_js())

    def _schedule(self, ms: int, action: Callable[[], None]) -> None:
        self._cancel()
        self._action, self._delay = action, ms
        if ms <= 0:
            action()
        else:
            self._timer = anki_compat.run_after(ms, action)
            if self._show_timer:
                anki_compat.reviewer_eval(build_countdown_js(ms / 1000))

    # --- reviewer events ------------------------------------------------------------
    def _on_question(self, card: Any) -> None:
        enabled, q_delay, _a_delay = self._delays_for(card)
        if not enabled:
            self._cancel()
            return
        self._schedule(q_delay, self._flip)

    def _on_answer(self, card: Any) -> None:
        enabled, _q_delay, a_delay = self._delays_for(card)
        if not enabled:
            self._cancel()
            return
        self._schedule(a_delay, self._grade)

    def _delays_for(self, card: Any) -> tuple[bool, int, int]:
        """Resolve (enabled, q_delay_ms, a_delay_ms) for ``card``'s deck via the settings."""
        settings = self._ctx.settings if self._ctx is not None else None
        if settings is None:
            return (False, 0, 0)
        deck_id = getattr(card, "did", None)
        return effective_delays(settings, deck_id)

    def _on_will_answer(self, ease: Any, *_args: Any) -> Any:
        # A manual (or our own) answer fired — drop any pending timer. Filter hook: pass
        # the ease through unchanged.
        self._cancel()
        return ease

    def _on_audio_end(self, *_args: Any) -> None:
        # Restart the current side's delay so it effectively counts from when audio ends.
        if self._wait_for_audio and self._action is not None:
            self._schedule(self._delay, self._action)

    # --- deck-list gear menu --------------------------------------------------------
    def _on_deck_menu(self, menu: Any, deck_id: int) -> None:
        # Add the per-deck "Omnia: Auto-Flip…" action to the deck's options menu. The
        # deck_options module is imported lazily: it subclasses QDialog (needs aqt.qt), so
        # importing it at module top would break the headless import of this feature.
        from omnia.features.auto_flip.deck_options import add_deck_menu_action

        if self._ctx is not None:
            add_deck_menu_action(menu, deck_id, self._ctx)

    # --- actions --------------------------------------------------------------------
    def _flip(self) -> None:
        if anki_compat.reviewer_side() == "question":
            anki_compat.reviewer_show_answer()

    def _grade(self) -> None:
        if anki_compat.reviewer_side() == "answer":
            anki_compat.reviewer_answer_card(_EASE_GOOD)
        # Terminal action — clear the fired timer/action so no stale state lingers. (_flip
        # must NOT do this: the resulting show-answer re-schedules via _on_answer.)
        self._cancel()
