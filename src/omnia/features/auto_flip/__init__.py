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
from omnia.features.auto_flip.logic import delay_ms

_EASE_GOOD = 3


@register("auto_flip")
class AutoFlipPlugin(FeaturePlugin):
    """Auto-advances the reviewer after configurable delays."""

    name = "Auto Flip"
    description = "Auto-advance question -> answer -> grade after a configurable delay."
    order = 10

    def __init__(self) -> None:
        self._timer: Optional[Any] = None
        self._action: Optional[Callable[[], None]] = None
        self._delay: int = 0
        self._subs: list[tuple[str, Callable[..., Any]]] = []
        self._q_delay = 0
        self._a_delay = 0
        self._wait_for_audio = False

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._q_delay = delay_ms(settings.delay_question_seconds)
        self._a_delay = delay_ms(settings.delay_answer_seconds)
        self._wait_for_audio = settings.wait_for_audio
        self._subscribe("reviewer_did_show_question", self._on_question)
        self._subscribe("reviewer_did_show_answer", self._on_answer)
        self._subscribe("reviewer_will_answer_card", self._on_will_answer)
        self._subscribe("av_player_did_end_playing", self._on_audio_end)

    def on_disable(self, ctx: PluginContext) -> None:
        self._cancel()
        for hook_name, callback in self._subs:
            anki_compat.unsubscribe_hook(hook_name, callback)
        self._subs.clear()

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

    def _schedule(self, ms: int, action: Callable[[], None]) -> None:
        self._cancel()
        self._action, self._delay = action, ms
        if ms <= 0:
            action()
        else:
            self._timer = anki_compat.run_after(ms, action)

    # --- reviewer events ------------------------------------------------------------
    def _on_question(self, _card: Any) -> None:
        self._schedule(self._q_delay, self._flip)

    def _on_answer(self, _card: Any) -> None:
        self._schedule(self._a_delay, self._grade)

    def _on_will_answer(self, ease: Any, *_args: Any) -> Any:
        # A manual (or our own) answer fired — drop any pending timer. Filter hook: pass
        # the ease through unchanged.
        self._cancel()
        return ease

    def _on_audio_end(self, *_args: Any) -> None:
        # Restart the current side's delay so it effectively counts from when audio ends.
        if self._wait_for_audio and self._action is not None:
            self._schedule(self._delay, self._action)

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
