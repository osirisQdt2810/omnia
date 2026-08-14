"""Tests for the shared hint widgets (``omnia.gui.widgets``).

Real Qt is not available headless, so ``QLabel``/``QFont``/``QPalette`` are stubbed with just
the surface these helpers touch. What is actually asserted is the decisions: that a hint reads
a step smaller than its surroundings, that a long explanation moves into the tooltip instead of
onto the dialog, and that the tooltip is width-limited and escaped.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

_qt = sys.modules["aqt.qt"]


class _FakeFont:
    """The four ``QFont`` calls ``_style_as_hint`` makes."""

    def __init__(self, point: float = 13.0, pixel: int = -1) -> None:
        self._point = point
        self._pixel = pixel

    def pointSizeF(self) -> float:
        return self._point

    def pixelSize(self) -> int:
        return self._pixel

    def setPointSizeF(self, value: float) -> None:
        self._point = value

    def setPixelSize(self, value: int) -> None:
        self._pixel = value


class _FakeLabel:
    """A ``QLabel`` reduced to what the helpers set on it."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.wrapped = False
        self.tooltip = ""
        self.style = ""
        self._font = _FakeFont()

    def setWordWrap(self, value: bool) -> None:
        self.wrapped = value

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def setStyleSheet(self, value: str) -> None:
        self.style = value

    def font(self) -> _FakeFont:
        return self._font

    def setFont(self, font: _FakeFont) -> None:
        self._font = font


class _FakeColor:
    def red(self) -> int:
        return 220

    def green(self) -> int:
        return 220

    def blue(self) -> int:
        return 220


class _FakeWidget:
    """Stands in for the dialog whose palette a hint derives its colour from."""

    def palette(self) -> Any:
        return types.SimpleNamespace(color=lambda _role: _FakeColor())


@pytest.fixture
def widgets(monkeypatch):
    """The module under test, with its Qt imports pointed at the fakes above."""
    monkeypatch.setattr(_qt, "QLabel", _FakeLabel, raising=False)
    monkeypatch.setattr(
        _qt,
        "QPalette",
        types.SimpleNamespace(ColorRole=types.SimpleNamespace(WindowText=object())),
        raising=False,
    )
    monkeypatch.setattr(_qt, "QWidget", _FakeWidget, raising=False)
    for name in [n for n in list(sys.modules) if n == "omnia.gui.widgets"]:
        del sys.modules[name]
    import omnia.gui.widgets as module

    return module


class TestHintLabel:
    def test_a_hint_reads_smaller_than_its_surroundings(self, widgets):
        # The reported problem: hints were the same size as the controls they described, so a
        # three-line explanation carried as much weight as the list above it.
        label = widgets.hint_label(_FakeWidget(), "some hint")

        assert label.font().pointSizeF() < 13.0
        assert label.wrapped is True
        assert "rgba(220, 220, 220," in label.style  # softened, not full-strength

    def test_a_pixel_sized_font_is_scaled_too(self, widgets, monkeypatch):
        # pointSizeF() is -1 when the font is defined in pixels; without the fallback a hint
        # would silently keep the full size on a platform that sizes fonts that way.
        label = widgets.hint_label(_FakeWidget(), "x")
        label._font = _FakeFont(point=-1.0, pixel=20)
        widgets._style_as_hint(_FakeWidget(), label)

        assert label.font().pixelSize() < 20


class TestHintWithDetails:
    def test_only_the_summary_is_shown_and_the_rest_is_a_tooltip(self, widgets):
        label = widgets.hint_with_details(
            _FakeWidget(),
            "Whole-word match.",
            "The long explanation.\n\nSecond paragraph.",
        )

        assert label.text.startswith("Whole-word match.")
        assert "long explanation" not in label.text  # it moved off the dialog
        assert "long explanation" in label.tooltip
        assert "Second paragraph" in label.tooltip

    def test_the_summary_advertises_that_there_is_more(self, widgets):
        # A tooltip nobody knows about is the same as no tooltip.
        label = widgets.hint_with_details(_FakeWidget(), "Short.", "Long.")

        assert label.text != "Short."
        assert label.text.startswith("Short.")

    def test_the_tooltip_is_width_limited(self, widgets):
        # Qt honours the longest line, so an unbounded tooltip runs a paragraph off-screen.
        label = widgets.hint_with_details(_FakeWidget(), "s", "d" * 400)

        assert "max-width" in label.tooltip

    def test_the_details_are_escaped(self, widgets):
        # The tooltip is rich text, so a literal < in an explanation must not become markup.
        label = widgets.hint_with_details(_FakeWidget(), "s", "a < b & c")

        assert "&lt;" in label.tooltip and "&amp;" in label.tooltip

    def test_no_details_falls_back_to_a_plain_hint(self, widgets):
        label = widgets.hint_with_details(_FakeWidget(), "Just this.", "   ")

        assert (
            label.text == "Just this."
        )  # no marker promising a tooltip that isn't there
        assert label.tooltip == ""
