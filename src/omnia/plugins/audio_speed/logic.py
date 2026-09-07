"""The speed rule, with no Anki in it.

Everything that can be wrong about a playback rate — drifting past its bounds, accumulating
float error step after step, or formatting as ``1.2000000000000002×`` — is decided here, so it
is decided once and unit-tested headless. The plugin only asks this object what the rate is
now and hands the answer to mpv and to the webview.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The speed every reset returns to. Not configurable: "reset" that lands somewhere other than
#: normal speed is not a reset, it is a second preset.
NORMAL_RATE = 1.0

#: The two sides a card's audio plays on, and the target that means "move both at once".
#: The two side names match the reviewer hooks (`on_question` / `on_answer`) so there is one
#: vocabulary from the hook to the tooltip; the UI calls them front and back.
QUESTION = "question"
ANSWER = "answer"
BOTH = "both"


@dataclass(frozen=True)
class SpeedBounds:
    """The window a rate may move in, and how far one press moves it.

    Validated on construction rather than at each use: a ``minimum`` above ``maximum`` would
    make every ``clamp`` return one of the two ends forever and look like a stuck shortcut.
    """

    minimum: float
    maximum: float
    step: float

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError(f"step must be positive, got {self.step}")
        if self.minimum > self.maximum:
            raise ValueError(f"minimum {self.minimum} is above maximum {self.maximum}")

    def clamp(self, rate: float) -> float:
        """``rate`` pulled inside the window, and rounded so repeated steps do not drift.

        Two decimals is the resolution the UI shows and mpv meaningfully honours; without the
        rounding, ten presses of 0.1 land on 1.9999999999999998 and the tooltip says so.
        """
        return round(min(self.maximum, max(self.minimum, rate)), 2)


class SpeedController:
    """Owns the current rate and the only three ways it may change.

    A plain float on the plugin would let any caller set anything; funnelling every change
    through ``up``/``down``/``reset``/``set`` means the bounds and the rounding are applied
    exactly once, at the one place a rate can be written.
    """

    def __init__(self, rate: float, bounds: SpeedBounds) -> None:
        self._bounds = bounds
        self._rate = bounds.clamp(rate)

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def bounds(self) -> SpeedBounds:
        return self._bounds

    def up(self) -> float:
        """One step faster, stopping at the maximum."""
        return self.set(self._rate + self._bounds.step)

    def down(self) -> float:
        """One step slower, stopping at the minimum."""
        return self.set(self._rate - self._bounds.step)

    def reset(self) -> float:
        """Back to normal speed (clamped, so a window that excludes 1.0 still resets sanely)."""
        return self.set(NORMAL_RATE)

    def set(self, rate: float) -> float:
        """Adopt ``rate`` after clamping and rounding; returns the rate actually adopted."""
        self._rate = self._bounds.clamp(rate)
        return self._rate

    def at_maximum(self) -> bool:
        return self._rate >= self._bounds.maximum

    def at_minimum(self) -> bool:
        return self._rate <= self._bounds.minimum


def format_rate(rate: float) -> str:
    """``1.5×``, ``2×``, ``0.75×`` — the shortest form that is still exact to two decimals."""
    text = f"{rate:.2f}".rstrip("0").rstrip(".")
    return f"{text}×"


class SideSpeeds:
    """The question side's rate and the answer side's, moved together or apart.

    Two rates rather than one because the two sides usually play different things. On the
    vocabulary decks this feature was built for, the question is a single word and the answer
    is a sentence read at length: the speed that keeps the word intelligible is not the speed
    that makes the sentence bearable, and one shared rate forces a compromise between them.

    :data:`BOTH` moves the two by the same STEP rather than to the same value. A user who has
    set the answer two notches faster than the question means that gap; collapsing it every
    time they pressed the plain shortcut would make the per-side settings useless.
    """

    def __init__(self, question: SpeedController, answer: SpeedController) -> None:
        self._by_side = {QUESTION: question, ANSWER: answer}

    def controller(self, side: str) -> SpeedController:
        """The controller for one side; the question side answers for an unknown name.

        An unknown side is not an error worth raising into a reviewer: the caller is a render
        hook or a menu action, and a wrong name should degrade to the side the user is most
        likely looking at rather than break the card.
        """
        return self._by_side.get(side, self._by_side[QUESTION])

    def rate_for(self, side: str) -> float:
        """The rate the given side plays at right now."""
        return self.controller(side).rate

    def rates(self) -> dict[str, float]:
        """Both rates, keyed by side — what gets persisted."""
        return {side: c.rate for side, c in self._by_side.items()}

    def matched(self) -> bool:
        """True when both sides play at the same rate (the shape a tooltip can shorten)."""
        return self._by_side[QUESTION].rate == self._by_side[ANSWER].rate

    def up(self, target: str = BOTH) -> None:
        """One step faster on ``target`` (a side, or BOTH)."""
        for controller in self._targets(target):
            controller.up()

    def down(self, target: str = BOTH) -> None:
        """One step slower on ``target`` (a side, or BOTH)."""
        for controller in self._targets(target):
            controller.down()

    def reset(self, target: str = BOTH) -> None:
        """Back to normal speed on ``target``."""
        for controller in self._targets(target):
            controller.reset()

    def _targets(self, target: str) -> list[SpeedController]:
        if target == BOTH:
            return [self._by_side[QUESTION], self._by_side[ANSWER]]
        return [self.controller(target)]


#: How each target reads in a tooltip. The UI says front/back; the code says question/answer.
_SIDE_LABEL = {QUESTION: "front", ANSWER: "back"}


def describe(speeds: SideSpeeds, target: str) -> str:
    """The sentence the tooltip shows after a change on ``target``.

    Names the side that moved, because a press can change a rate the user cannot hear yet —
    bumping the answer side while the question is on screen changes nothing audible until the
    flip, and a tooltip that just said "1.5×" would look like it had done nothing.
    """
    if target != BOTH:
        rate = format_rate(speeds.rate_for(target))
        return f"Audio speed {rate} ({_SIDE_LABEL.get(target, target)})"
    if speeds.matched():
        return f"Audio speed {format_rate(speeds.rate_for(QUESTION))}"
    front = format_rate(speeds.rate_for(QUESTION))
    back = format_rate(speeds.rate_for(ANSWER))
    return f"Audio speed — front {front}, back {back}"
