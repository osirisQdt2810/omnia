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


def rich_tooltip(text: str, width_px: int = 340) -> str:
    """Return ``text`` as a width-limited HTML tooltip that KEEPS its line breaks.

    Two separate Qt behaviours have to be handled together, and handling only one is worse than
    handling neither:

    * a tooltip Qt reads as rich text is laid out with ``white-space: normal``, so every run of
      whitespace — a blank line between paragraphs included — collapses to a single space. The
      breaks have to become ``<br>`` explicitly. (This bit the first version of
      :func:`hint_with_details`: it escaped the text, which made Qt treat it as rich text, and
      the authored paragraphs shipped as one run-on block.)
    * an unbounded tooltip is laid out to its LONGEST line, so a paragraph renders as one strip
      running off the edge of the screen. Hence the width cap.

    Promoted here from ``config_form`` because a third copy of this pairing is a third chance to
    get one half of it right and the other wrong.

    Args:
        text: Plain text, with ``\n`` where a break is intended.
        width_px: Maximum rendered width.

    Returns:
        HTML safe to hand to ``setToolTip``.
    """
    body = html.escape(text).replace("\n", "<br>")
    return f'<div style="max-width:{width_px}px">{body}</div>'


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
    label.setToolTip(rich_tooltip(details))
    _style_as_hint(widget, label)
    return label
