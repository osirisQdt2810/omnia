"""Small Qt helpers shared by more than one Omnia dialog.

Anything here has at least two callers by construction — a widget used by a single dialog
belongs in that dialog's module. Today that is the secondary-text hint label, whose theme
trick has to be identical everywhere: a dialog that reimplements it and gets the colour
direction wrong goes unreadable on exactly one of Anki's two themes.
"""

from __future__ import annotations

from aqt.qt import QLabel, QPalette, QWidget  # type: ignore[attr-defined]

# How much of the window's text colour a hint keeps (0-255) — enough to read, clearly
# secondary next to the surrounding labels.
_HINT_ALPHA = 165


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
    color = widget.palette().color(QPalette.ColorRole.WindowText)
    label.setStyleSheet(
        f"color: rgba({color.red()}, {color.green()}, {color.blue()}, {_HINT_ALPHA});"
    )
    return label
