"""Small Qt helpers shared by more than one Omnia dialog.

Anything here has at least two callers by construction — a widget used by a single dialog
belongs in that dialog's module. Today that is the secondary-text hint label, whose theme
trick has to be identical everywhere: a dialog that reimplements it and gets the colour
direction wrong goes unreadable on exactly one of Anki's two themes.
"""

from __future__ import annotations

import html

from aqt.qt import QLabel, QPalette, QWidget  # type: ignore[attr-defined]

# How much of the window's text colour a hint keeps (0-255) — enough to read, clearly
# secondary next to the surrounding labels.
_HINT_ALPHA = 165

# Hints render a step down from the surrounding labels. They were the same size, so a
# three-line explanation under a list carried as much visual weight as the control it
# described and the dialog read as a wall of text. Relative, not absolute: Anki's base font
# size is a user setting, and a hardcoded pt would fight it.
_HINT_SCALE = 0.87

# Appended to a summary that has more to say, so the tooltip is discoverable — a hint that
# only reveals itself on an accidental hover may as well not exist.
_MORE = " ⓘ"


def hint_label(widget: QWidget, text: str) -> QLabel:
    """Return a secondary-text label that stays readable in BOTH themes.

    ``palette(mid)`` (the obvious choice) resolves to a near-black under Anki's dark theme,
    which is invisible on its dark background. Deriving from the window's ACTUAL text colour
    and softening it with alpha keeps the contrast direction correct whatever the theme.

    Args:
        widget: The widget whose palette the colour is derived from (usually the dialog).
        text: The hint to show (word-wrapped).

    Returns:
        The configured :class:`QLabel` — the caller adds it to its layout.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    _style_as_hint(widget, label)
    return label


def _style_as_hint(widget: QWidget, label: QLabel) -> None:
    """Apply the shared secondary-text look: softened colour, one step down in size."""
    color = widget.palette().color(QPalette.ColorRole.WindowText)
    font = label.font()
    # pointSizeF is -1 when the font is defined in pixels; fall back to pixelSize so a hint
    # never silently keeps the full size on a platform that sizes fonts that way.
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * _HINT_SCALE)
    elif font.pixelSize() > 0:
        font.setPixelSize(max(1, int(font.pixelSize() * _HINT_SCALE)))
    label.setFont(font)
    label.setStyleSheet(
        f"color: rgba({color.red()}, {color.green()}, {color.blue()}, {_HINT_ALPHA});"
    )


def hint_with_details(widget: QWidget, summary: str, details: str) -> QLabel:
    """Return a ONE-LINE hint whose full explanation lives in its tooltip.

    The settings dialogs had grown three- and four-line explanations printed under every
    control — the rules for whole-word matching, what an empty list means, which forms count
    as the same word. All of it is true and worth saying, and printed in full it buried the
    controls it described.

    So the label states the point in a phrase and the detail is one hover away. The tooltip is
    word-wrapped by Qt's rich text (a long single line otherwise renders as one unreadable
    strip across the screen), and the summary carries a marker so the reader knows there IS
    more — a tooltip nobody knows about is the same as no tooltip.

    Args:
        widget: The widget whose palette the colour is derived from (usually the dialog).
        summary: The short line to show. Keep it to a phrase.
        details: The full explanation, shown on hover. Blank falls back to a plain hint.

    Returns:
        The configured :class:`QLabel` — the caller adds it to its layout.
    """
    if not details.strip():
        return hint_label(widget, summary)
    label = QLabel(summary + _MORE)
    label.setWordWrap(True)
    # Rich text so Qt wraps it; width-limited because Qt will otherwise honour the longest
    # line and run a paragraph off the edge of the screen.
    label.setToolTip(f'<div style="max-width:340px">{html.escape(details)}</div>')
    _style_as_hint(widget, label)
    return label
