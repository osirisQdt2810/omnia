"""Audio speed: play card audio faster or slower with a shortcut, on BOTH sides, and keep it.

Why this is an Omnia plugin and not a shortcut on top of the third-party "Audio Playback
Controls" add-on: that add-on sets mpv's speed (which persists) and then patches
``playbackRate`` on whatever ``<audio>`` elements exist at the moment a key is pressed. A
template that plays its answer-side audio through its own ``<audio>`` element — which is how
the reporting deck does it — renders fresh elements on the flip, so the question side sped up
and the answer side did not. The rate has to be re-applied on every render, to every player,
from a value that survives the flip. The web-injector seam already runs JS on both sides, so
the persistent half lives here and the per-render half is one injected script.

Two players, two paths, one rate:

* ``[sound:...]`` tags → Anki's mpv process → :func:`anki_compat.set_mpv_speed`. mpv keeps its
  ``speed`` property across clips, so this is set on enable and on every change, not per card.
* ``<audio>``/``<video>`` a template plays itself → ``speed.js``, injected on both sides and
  fed the current rate on every render by a dynamic provider.

Degrades cleanly where mpv is not the player (Qt player chosen, or a Windows install without
mpv): the mpv call reports False, the HTML path still works, and nothing raises in the reviewer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.gui import audio_speed as _audio_speed_gui
from omnia.gui.assets import read_asset
from omnia.plugins.audio_speed.config import AudioSpeedSettings
from omnia.plugins.audio_speed.logic import (
    SpeedBounds,
    SpeedController,
    format_rate,
)

logger = logging.getLogger(__name__)

_ACTION_SPEED_UP = "Audio: speed up"
_ACTION_SLOW_DOWN = "Audio: slow down"
_ACTION_RESET = "Audio: reset speed"


def build_speed_js() -> str:
    """The idempotent applier script, injected on both reviewer sides."""
    return read_asset(_audio_speed_gui.__file__, "web", "speed.js").strip()


def push_rate_js(rate: float) -> str:
    """One-liner that hands ``rate`` to the applier, whichever of the two ran first.

    The static asset and this dynamic snippet are both injected per render and their relative
    order is the injector's business, not ours. If the applier exists, apply now; if not, park
    the rate where the applier's installer will find it.
    """
    return (
        "(function(r){var S=window.__omniaAudioSpeed;"
        "if(S&&S.apply){S.apply(r);}else{window.__omniaAudioSpeedPending=r;}"
        f"}})({rate!r});"
    )


@register("audio_speed")
class AudioSpeedPlugin(FeaturePlugin):
    """Speed up / slow down / reset card audio with shortcuts; the speed persists."""

    id = "audio_speed"
    name = "Audio Speed"
    description = (
        "Play card audio faster or slower with a shortcut — on both sides, for [sound:] "
        "clips and for audio the template plays itself — and remember the speed."
    )
    group = "Reviewing"
    tooltip = (
        "Default ] speeds up, [ slows down, Ctrl+] resets. Works on both the question and the "
        "answer side, unlike per-element speed add-ons, and the speed is kept between sessions."
    )
    order = 45
    config_model = AudioSpeedSettings

    def __init__(self) -> None:
        super().__init__()
        self._ctx: Optional[PluginContext] = None
        self._controller: Optional[SpeedController] = None
        self._actions: list[Any] = []
        self._show_tooltip = True
        self._remember = True

    # -- lifecycle ------------------------------------------------------------------------

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._ctx = ctx
        self._show_tooltip = bool(getattr(settings, "show_tooltip", True))
        self._remember = bool(getattr(settings, "remember_rate", True))
        start = float(settings.rate) if self._remember else 1.0
        self._controller = SpeedController(
            start,
            SpeedBounds(
                minimum=float(settings.min_rate),
                maximum=float(settings.max_rate),
                step=float(settings.step),
            ),
        )

        web = getattr(ctx, "web", None)
        if web is not None:
            js = build_speed_js()
            web.add_asset(self.id, WebAsset(question_js=js, answer_js=js))
            # The CURRENT rate on every render — this is the half the old add-on lacked.
            web.add_dynamic(
                self.id, on_question=self._rate_for_card, on_answer=self._rate_for_card
            )

        self._actions = [
            anki_compat.add_tools_menu_action(
                _ACTION_SPEED_UP,
                self._make_handler(lambda c: c.up()),
                shortcut=settings.speed_up_shortcut or None,
            ),
            anki_compat.add_tools_menu_action(
                _ACTION_SLOW_DOWN,
                self._make_handler(lambda c: c.down()),
                shortcut=settings.slow_down_shortcut or None,
            ),
            anki_compat.add_tools_menu_action(
                _ACTION_RESET,
                self._make_handler(lambda c: c.reset()),
                shortcut=settings.reset_shortcut or None,
            ),
        ]
        # mpv keeps this until told otherwise; set once here, then only on change.
        self._apply(announce=False)

    def on_disable(self, ctx: PluginContext) -> None:
        web = getattr(ctx, "web", None)
        if web is not None:
            web.remove(
                self.id
            )  # drops the asset, the dynamic provider and any handlers
        for action in self._actions:
            anki_compat.remove_tools_menu_action(action)
        self._actions = []
        # Leave mpv the way Anki expects it; a disabled speed plugin must not keep 1.7×.
        anki_compat.set_mpv_speed(1.0)
        self._controller = None
        self._ctx = None

    # -- the one write path --------------------------------------------------------------

    def _make_handler(
        self, change: Callable[[SpeedController], float]
    ) -> Callable[[bool], None]:
        """Wrap a controller move as a Tools-menu callback (which receives a checked flag)."""

        def handler(_checked: bool = False) -> None:
            if self._controller is None:
                return
            change(self._controller)
            self._apply(announce=True)
            self._persist()

        return handler

    def _apply(self, *, announce: bool) -> None:
        """Push the controller's rate to mpv and to the live webview, then tell the user."""
        if self._controller is None:
            return
        rate = self._controller.rate
        mpv_ok = anki_compat.set_mpv_speed(rate)
        try:
            anki_compat.reviewer_eval(push_rate_js(rate))
        except Exception:  # no reviewer up yet; the next render's dynamic JS carries it
            logger.debug("audio_speed: no reviewer webview to push %s× into", rate)
        if announce and self._show_tooltip:
            suffix = (
                "" if mpv_ok else " (HTML audio only — mpv is not the active player)"
            )
            anki_compat.show_tooltip(f"Audio speed {format_rate(rate)}{suffix}")

    def _persist(self) -> None:
        """Write the rate back so the next session starts here; a shallow one-key merge."""
        if not self._remember or self._ctx is None or self._controller is None:
            return
        config = getattr(self._ctx, "config", None)
        if config is None:
            return
        try:
            config.update_section(self.id, {"rate": self._controller.rate})
        except (
            Exception
        ):  # a failed save must never break the shortcut that triggered it
            logger.exception("audio_speed: could not persist rate")

    def _rate_for_card(self, _card: Any) -> str:
        """Dynamic-JS provider: the current rate, every render, both sides."""
        if self._controller is None:
            return ""
        return push_rate_js(self._controller.rate)
