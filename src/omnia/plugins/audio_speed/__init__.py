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
    ANSWER,
    BOTH,
    QUESTION,
    SideSpeeds,
    SpeedBounds,
    SpeedController,
    describe,
)

logger = logging.getLogger(__name__)

# Every entry this plugin puts in Anki's Tools menu carries the add-on's name, because that
# menu belongs to Anki and to every add-on the user has installed: seven bare "Audio: …" lines
# in it say nothing about where they came from or which settings screen turns them off. Matches
# the shape Auto-Flip already uses.
_MENU_PREFIX = "Omnia · Audio"

_ACTION_SPEED_UP = f"{_MENU_PREFIX}: speed up"
_ACTION_SLOW_DOWN = f"{_MENU_PREFIX}: slow down"
_ACTION_RESET = f"{_MENU_PREFIX}: reset speed"
_ACTION_FRONT_UP = f"{_MENU_PREFIX}: speed up (front side)"
_ACTION_FRONT_DOWN = f"{_MENU_PREFIX}: slow down (front side)"
_ACTION_BACK_UP = f"{_MENU_PREFIX}: speed up (back side)"
_ACTION_BACK_DOWN = f"{_MENU_PREFIX}: slow down (back side)"


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
        "Play card audio faster or slower with a shortcut — for [sound:] clips and for audio "
        "the template plays itself — with a separate speed for the front and the back."
    )
    group = "Reviewing"
    tooltip = (
        "Default ] speeds up and [ slows down — both sides at once, by one step each, so any "
        "difference you set between them is kept. Ctrl+] resets both.\n"
        "Alt+] / Alt+[ move only the FRONT side, Shift+] / Shift+[ only the BACK — a one-word "
        "prompt and a long example sentence rarely want the same speed.\n"
        "Both sides are covered, unlike per-element speed add-ons, and the speeds are kept "
        "between sessions."
    )
    order = 45
    config_model = AudioSpeedSettings

    def __init__(self) -> None:
        super().__init__()
        self._ctx: Optional[PluginContext] = None
        self._speeds: Optional[SideSpeeds] = None
        self._actions: list[Any] = []
        self._show_tooltip = True
        self._remember = True
        # Which side is on screen. A shortcut changes a stored rate; only the side being
        # rendered can be made to sound different right now, so this decides what mpv and the
        # live page are told. Starts on the question side because that is what a card opens on.
        self._side = QUESTION

    # -- lifecycle ------------------------------------------------------------------------

    def on_enable(self, ctx: PluginContext) -> None:
        settings = ctx.settings
        self._ctx = ctx
        self._show_tooltip = bool(getattr(settings, "show_tooltip", True))
        self._remember = bool(getattr(settings, "remember_rate", True))
        # Sorted, not trusted in order: the settings form has no cross-field validation, so a
        # user who raises min_rate before max_rate writes a crossed pair. Sorting turns that
        # into the window they meant; rejecting it would make the section unreadable and take
        # the panel down with it, leaving no way to correct the mistake in the UI.
        low, high = sorted((float(settings.min_rate), float(settings.max_rate)))
        bounds = SpeedBounds(minimum=low, maximum=high, step=float(settings.step))

        # Upgrading from the single-rate version: the stored section has `rate` and no
        # `answer_rate`, and defaulting the answer side to 1.0 would silently halve the speed
        # of every back side the user had been listening to. Adopt the old rate for both.
        inherited = "answer_rate" not in getattr(settings, "__fields_set__", set())
        question_start = float(settings.rate) if self._remember else 1.0
        answer_start = question_start if inherited else float(settings.answer_rate)
        if not self._remember:
            answer_start = 1.0
        self._speeds = SideSpeeds(
            SpeedController(question_start, bounds),
            SpeedController(answer_start, bounds),
        )
        if inherited and self._remember:
            # Write the adopted value out NOW rather than waiting for a shortcut. Until the key
            # exists, the generic settings form shows the field's 1.0 default, and saving that
            # form would write the default over the speed the user is actually hearing.
            self._persist()

        web = getattr(ctx, "web", None)
        if web is not None:
            js = build_speed_js()
            web.add_asset(self.id, WebAsset(question_js=js, answer_js=js))
            # The CURRENT rate on every render — this is the half the old add-on lacked.
            # One provider per side: each records which side is now on screen and returns the
            # rate for that side, so the flip itself is what switches speeds.
            web.add_dynamic(
                self.id,
                on_question=self._question_js,
                on_answer=self._answer_js,
            )

        # Seven actions: the three that move both sides, and two per side. Every one is the
        # same handler shape — a target and a direction — so the menu stays a table.
        moves: list[tuple[str, str, str, str]] = [
            (_ACTION_SPEED_UP, BOTH, "up", settings.speed_up_shortcut),
            (_ACTION_SLOW_DOWN, BOTH, "down", settings.slow_down_shortcut),
            (_ACTION_RESET, BOTH, "reset", settings.reset_shortcut),
            (_ACTION_FRONT_UP, QUESTION, "up", settings.question_up_shortcut),
            (_ACTION_FRONT_DOWN, QUESTION, "down", settings.question_down_shortcut),
            (_ACTION_BACK_UP, ANSWER, "up", settings.answer_up_shortcut),
            (_ACTION_BACK_DOWN, ANSWER, "down", settings.answer_down_shortcut),
        ]
        # Ask BEFORE binding: a sequence another add-on already holds is not a conflict Qt
        # resolves, it is one Qt refuses — both actions go ambiguous and neither fires. The
        # user then has a dead key and nothing anywhere says why. The add-on this plugin
        # replaces ships `]` and `[` as its own defaults, so anyone who has not removed it yet
        # lands exactly here.
        taken = {
            shortcut: anki_compat.shortcut_already_taken(shortcut)
            for _label, _target, _move, shortcut in moves
            if shortcut
        }
        self._actions = [
            anki_compat.add_tools_menu_action(
                label,
                self._make_handler(target, move),
                shortcut=shortcut or None,
            )
            for label, target, move, shortcut in moves
        ]
        self._warn_about_taken_shortcuts(taken)
        # mpv keeps this until told otherwise; set once here, then per render and per change.
        self._apply(BOTH, announce=False)

    def on_disable(self, ctx: PluginContext) -> None:
        web = getattr(ctx, "web", None)
        if web is not None:
            web.remove(
                self.id
            )  # drops the asset, the dynamic provider and any handlers
        for action in self._actions:
            anki_compat.remove_tools_menu_action(action)
        self._actions = []
        # Both players have to be handed back, not just mpv. Dropping the injector entry only
        # stops FUTURE renders from carrying a rate; the applier installed in the page that is
        # on screen right now keeps forcing the old rate on every <audio> the template creates,
        # until the reviewer webview is rebuilt. Zero resets everything to 1.0 and then stands
        # the applier down — the prototype wrap cannot be removed, but it can be made inert, so
        # a template that sets its own rate is left alone.
        self._push_rate(0.0)
        # Leave mpv the way Anki expects it; a disabled speed plugin must not keep 1.7×.
        anki_compat.set_mpv_speed(1.0)
        self._speeds = None
        self._side = QUESTION
        self._ctx = None

    @staticmethod
    def _warn_about_taken_shortcuts(taken: dict[str, list[str]]) -> None:
        """Tell the user which shortcuts will not work, and who is holding them.

        Said once, at enable, because there is no later moment: pressing the key produces
        nothing at all, so a user who has not been told has no thread to pull. The message
        names the other action rather than guessing at an add-on, since that is the only thing
        Qt actually knows.
        """
        clashes = {key: names for key, names in taken.items() if names}
        if not clashes:
            return
        detail = "; ".join(
            f"{key} is already used by {' and '.join(names)}"
            for key, names in sorted(clashes.items())
        )
        logger.warning("audio_speed: shortcut(s) already bound — %s", detail)
        try:
            anki_compat.show_tooltip(
                f"Audio Speed: {detail}. Those keys will do nothing until the other add-on "
                "is removed — Tools → Add-ons."
            )
        except Exception:
            logger.debug("audio_speed: could not show the shortcut-clash tooltip")

    # -- the one write path --------------------------------------------------------------

    def _make_handler(self, target: str, move: str) -> Callable[[bool], None]:
        """Wrap one menu action — a target side and a direction — as a Qt callback.

        Args:
            target: :data:`QUESTION`, :data:`ANSWER`, or :data:`BOTH`.
            move: ``"up"``, ``"down"`` or ``"reset"``.
        """

        def handler(_checked: bool = False) -> None:
            speeds = self._speeds
            if speeds is None:
                return
            before = speeds.rates()
            getattr(speeds, move)(target)
            self._apply(target, announce=True)
            # Holding the key auto-repeats; at a bound every repeat clamps to the same rate,
            # and each save is a collection-backed write that syncs. Only save a real change.
            if speeds.rates() != before:
                self._persist()

        return handler

    def _apply(self, target: str, *, announce: bool) -> None:
        """Make the side on screen sound right, then say what changed.

        Only the rendered side can be made audible now — a press that moved the other side is
        stored and takes effect at the flip — so mpv and the live page always get
        ``self._side``'s rate, while the tooltip reports ``target``, which is what the user
        actually pressed.
        """
        speeds = self._speeds
        if speeds is None:
            return
        mpv_ok = anki_compat.set_mpv_speed(speeds.rate_for(self._side))
        self._push_rate(speeds.rate_for(self._side))
        if announce and self._show_tooltip:
            suffix = (
                "" if mpv_ok else " (HTML audio only — mpv is not the active player)"
            )
            anki_compat.show_tooltip(f"{describe(speeds, target)}{suffix}")

    def _push_rate(self, rate: float) -> None:
        """Feed a rate to the applier in the page that is on screen, if there is one."""
        try:
            anki_compat.reviewer_eval(push_rate_js(rate))
        except Exception:  # no reviewer up yet; the next render's dynamic JS carries it
            logger.debug("audio_speed: no reviewer webview to push %s× into", rate)

    def _persist(self) -> None:
        """Write both rates back so the next session starts here; a shallow two-key merge."""
        if not self._remember or self._ctx is None or self._speeds is None:
            return
        config = getattr(self._ctx, "config", None)
        if config is None:
            return
        rates = self._speeds.rates() if self._speeds else {}
        try:
            config.update_section(
                self.id,
                {"rate": rates[QUESTION], "answer_rate": rates[ANSWER]},
            )
        except (
            Exception
        ):  # a failed save must never break the shortcut that triggered it
            logger.exception("audio_speed: could not persist rate")

    def _question_js(self, _card: Any) -> str:
        """Dynamic-JS provider for the question side."""
        return self._enter_side(QUESTION)

    def _answer_js(self, _card: Any) -> str:
        """Dynamic-JS provider for the answer side."""
        return self._enter_side(ANSWER)

    def _enter_side(self, side: str) -> str:
        """Adopt ``side``'s rate for this render and return the snippet that carries it.

        This is where the two rates actually become two speeds: the flip re-renders, this runs,
        and both players are handed the rate belonging to the side now on screen.

        It also re-asserts the rate on mpv every render. ``speed`` is per-process state and
        Anki restarts mpv when it dies (``MpvManager.on_init`` exists for exactly that), so a
        rate set once at enable time can quietly go back to 1× mid-session. Re-setting it here
        is one local IPC call on a hop Anki already makes.
        """
        if self._speeds is None:
            return ""
        self._side = side
        rate = self._speeds.rate_for(side)
        anki_compat.set_mpv_speed(rate)
        return push_rate_js(rate)
