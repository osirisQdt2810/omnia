"""Auto Flip feature: auto-advance question -> answer -> grade after a delay.

A faithful port of the "Automatically flip cards 2" add-on onto Omnia's seams. Each
reviewer side schedules a one-shot timer; the question side flips to the answer and the
answer side auto-grades Good (routed through the ease pipeline, so it cooperates with
typed_accuracy / overdue_guard). On top of the plain delay this feature adds:

* **Audio-aware arming** (``wait_for_audio``): the timer only arms once a card's audio has
  finished — it starts immediately only when a side plays no sounds, otherwise it arms when
  ``av_player`` drains.
* **mpv ``--range=`` extension**: a side whose audio is an external "myview.mpv" clip carries
  the clip duration in its command; that duration is added to the wait so a clip never gets
  cut off.
* **Ctrl+J runtime toggle**: a checkable Tools-menu action suspends/resumes auto-flip without
  disabling the plugin (a runtime ``_active`` flag, distinct from plugin-enabled).
* **Two-stage Enter cancel**: the first Enter while a timer is pending cancels it (the
  countdown ring turns "cancelled") without flipping/grading; a second Enter performs the
  real action.

The countdown overlay (``countdown.py``) and grading-through-the-ease-pipeline are Omnia
improvements kept over the reference's bottom-bar recolor / ``onEnterKey`` grade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.auto_flip.config import AutoFlipSettings
from omnia.plugins.auto_flip.countdown import (
    build_countdown_js,
    clear_countdown_js,
    mark_countdown_cancelled_js,
)
from omnia.plugins.auto_flip.logic import (
    effective_delays,
    parse_mpv_range_extra_seconds,
)

_EASE_GOOD = 3
_DECK_MENU_HOOK = "deck_browser_will_show_options_menu"
_TOOLS_ACTION_LABEL = "Auto-Flip (Ctrl+J)"
_TOOLS_ACTION_SHORTCUT = "Ctrl+J"


@register("auto_flip")
class AutoFlipPlugin(FeaturePlugin):
    """Auto-advances the reviewer after configurable delays."""

    name = "Auto Flip"
    description = "Auto-advance question -> answer -> grade after a configurable delay."
    group = "Reviewing"
    order = 10
    config_model = AutoFlipSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._timer: Optional[Any] = None
        self._action: Optional[Callable[[], None]] = None
        self._delay: int = 0
        self._subs: list[tuple[str, Callable[..., Any]]] = []
        self._wait_for_audio = False
        self._show_timer = True
        # Runtime suspend/resume (Ctrl+J), independent of the plugin being enabled.
        self._active = True
        self._tools_action: Optional[Any] = None
        # Which side is currently armed/awaiting audio ("question" | "answer" | None).
        self._pending_side: Optional[str] = None
        # First Enter while a timer is pending cancels it; the second Enter acts for real.
        self._enter_cancelled = False

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._ctx = ctx
        self._wait_for_audio = settings.wait_for_audio
        self._show_timer = settings.show_timer
        self._active = True
        self._subscribe("reviewer_will_answer_card", self._on_will_answer)
        self._subscribe(_DECK_MENU_HOOK, self._on_deck_menu)
        if self._wait_for_audio:
            # Arm off the audio hooks so a card with sound never flips before it finishes.
            self._subscribe(
                "reviewer_will_play_question_sounds", self._on_question_sounds
            )
            self._subscribe("reviewer_will_play_answer_sounds", self._on_answer_sounds)
            self._subscribe("av_player_did_end_playing", self._on_audio_end)
        else:
            self._subscribe("reviewer_did_show_question", self._on_question)
            self._subscribe("reviewer_did_show_answer", self._on_answer)
        self._tools_action = anki_compat.add_tools_menu_action(
            _TOOLS_ACTION_LABEL,
            self._on_toggle,
            checkable=True,
            checked=self._active,
            shortcut=_TOOLS_ACTION_SHORTCUT,
        )
        anki_compat.wrap_reviewer_enter_key(self._on_enter_key)

    def on_disable(self, ctx: PluginContext) -> None:
        self._cancel()
        anki_compat.restore_reviewer_enter_key()
        anki_compat.remove_tools_menu_action(self._tools_action)
        self._tools_action = None
        for hook_name, callback in self._subs:
            anki_compat.unsubscribe_hook(hook_name, callback)
        self._subs.clear()
        self._ctx = None

    # --- scheduling -----------------------------------------------------------------
    def _subscribe(self, hook_name: str, callback: Callable[..., Any]) -> None:
        anki_compat.subscribe_hook(hook_name, callback)
        self._subs.append((hook_name, callback))

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._action = None
        self._pending_side = None
        self._enter_cancelled = False
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

    # --- reviewer events (no-audio path: arm on show) -------------------------------
    def _on_question(self, card: Any) -> None:
        self._arm_question(card)

    def _on_answer(self, card: Any) -> None:
        self._arm_answer(card)

    # --- reviewer events (audio path: arm only once sounds are absent/finished) -----
    def _on_question_sounds(self, card: Any, sounds: Any) -> None:
        self._pending_side = "question"
        # Arm now only when this side plays nothing; otherwise wait for av_player to drain.
        if not sounds:
            self._arm_question(card)

    def _on_answer_sounds(self, card: Any, sounds: Any) -> None:
        self._pending_side = "answer"
        if not sounds:
            self._arm_answer(card)

    def _on_audio_end(self, *_args: Any) -> None:
        # Audio finished — arm the side we were waiting on (counts the delay from now).
        if not self._wait_for_audio or self._enter_cancelled:
            return
        if anki_compat.audio_still_playing():
            return  # more clips still queued; arm only when the queue is empty
        card = anki_compat.current_card()
        if card is None:
            return
        side = anki_compat.reviewer_side()
        if side == "question" and self._pending_side == "question":
            self._arm_question(card)
        elif side == "answer" and self._pending_side == "answer":
            self._arm_answer(card)

    # --- arming helpers -------------------------------------------------------------
    def _arm_question(self, card: Any) -> None:
        if not self._active:
            self._cancel()
            return
        enabled, q_delay, _a_delay = self._delays_for(card)
        if not enabled:
            self._cancel()
            return
        extra_ms = self._mpv_extra_ms(card, "question")
        self._pending_side = "question"
        self._schedule(q_delay + extra_ms, self._flip)

    def _arm_answer(self, card: Any) -> None:
        if not self._active:
            self._cancel()
            return
        enabled, _q_delay, a_delay = self._delays_for(card)
        if not enabled:
            self._cancel()
            return
        extra_ms = self._mpv_extra_ms(card, "answer")
        self._pending_side = "answer"
        self._schedule(a_delay + extra_ms, self._grade)

    def _delays_for(self, card: Any) -> tuple[bool, int, int]:
        """Resolve (enabled, q_delay_ms, a_delay_ms) for ``card``'s home deck."""
        settings = self._ctx.settings if self._ctx is not None else None
        if settings is None:
            return (False, 0, 0)
        # Filtered/cram cards: key the override off the original (home) deck, not the
        # temporary filtered deck — matching how Anki resolves per-deck config.
        deck_id = anki_compat.effective_deck_id(card)
        return effective_delays(settings, deck_id)

    def _mpv_extra_ms(self, card: Any, side: str) -> int:
        """Extra wait (ms) so an external mpv ``--range=`` clip isn't cut off."""
        text = anki_compat.card_side_av_text(card, side)
        return int(parse_mpv_range_extra_seconds(text) * 1000)

    def _on_will_answer(self, ease: Any, *_args: Any) -> Any:
        # A manual (or our own) answer fired — drop any pending timer. Filter hook: pass
        # the ease through unchanged.
        self._cancel()
        return ease

    # --- Ctrl+J runtime toggle ------------------------------------------------------
    def _on_toggle(self, active: bool) -> None:
        self._active = active
        if not active:
            self._cancel()
            return
        # Re-arm the current side immediately (the reference resumes the live card).
        card = anki_compat.current_card()
        if card is None:
            return
        side = anki_compat.reviewer_side()
        if side == "question":
            self._arm_question(card)
        elif side == "answer":
            self._arm_answer(card)

    # --- two-stage Enter cancel -----------------------------------------------------
    def _on_enter_key(self, _reviewer: Any, original: Callable[[], None]) -> None:
        # No pending timer (or feature suspended) -> behave exactly like Anki's Enter.
        if not self._active or self._timer is None:
            original()
            return
        if not self._enter_cancelled:
            # First Enter: cancel the pending auto-flip and mark the ring, but don't act.
            self._enter_cancelled = True
            self._timer.stop()
            self._timer = None
            self._action = None
            anki_compat.reviewer_eval(mark_countdown_cancelled_js())
            return
        # Second Enter: perform Anki's real action.
        original()

    # --- deck-list gear menu --------------------------------------------------------
    def _on_deck_menu(self, menu: Any, deck_id: int) -> None:
        # Add the per-deck "Omnia: Auto-Flip…" action to the deck's options menu. The
        # deck_options module is imported lazily: it subclasses QDialog (needs aqt.qt), so
        # importing it at module top would break the headless import of this feature.
        from omnia.gui.auto_flip.deck_options import add_deck_menu_action

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
        # must NOT do this: the resulting show-answer re-schedules via the answer arm.)
        self._cancel()
