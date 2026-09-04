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
