"""Presentation metadata for the settings-page plugin CATEGORIES (icon, blurb, accent).

Purely visual, so it lives in ``gui`` — ``core`` must never import ``gui``, and ``core`` has
no business knowing an SVG path. The *ordering* of categories stays in
``core.manager.GROUP_ORDER`` (a data concern of ``group_plugins``); every name listed there
MUST have an entry here — ``tests/gui/test_settings_categories.py`` pins that.

Nothing here is derived from a plugin: a category is a *group name*, and the page stays
generic over the registry (ADR-002, ADR-011/012). A plugin declaring an unlisted group still
renders — it falls back to :data:`DEFAULT_CATEGORY_STYLE`, whose accents point at the theme
variables so it matches whichever palette is active.

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryStyle:
    """How one category is painted on the landing grid.

    Attributes:
        icon: SVG path data (the ``d`` attribute) drawn in a 24x24 viewBox, stroked with
            ``currentColor``. A TRUSTED constant — emitted raw into the page, so nothing
            derived from a plugin or from config may ever be assigned here.
        blurb: One line, shown on the tile and in the category header.
        accent_from: Gradient start; either ``#rrggbb`` or a ``var(--…)`` theme reference.
        accent_to: Gradient end; same two forms as ``accent_from``.
    """

    icon: str
    blurb: str
    accent_from: str
    accent_to: str


CATEGORY_STYLES: dict[str, CategoryStyle] = {
    "Reviewing": CategoryStyle(
        icon=(
            "M12 2 2 7l10 5 10-5-10-5z M2 17l10 5 10-5 M2 12l10 5 10-5"
        ),  # stacked cards
        blurb="What happens while you review a card.",
        accent_from="#5b6ef5",
        accent_to="#8a5cf6",
    ),
    "Grading": CategoryStyle(
        icon=(
            "M22 11.08V12a10 10 0 1 1-5.93-9.14 M22 4 12 14.01l-3-3"
        ),  # check in a circle
        blurb="How an answer turns into an ease.",
        accent_from="#10b981",
        accent_to="#22d3ee",
    ),
    "AI": CategoryStyle(
        icon=(
            "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z "
            "M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z"
        ),  # sparkles
        blurb="Generate and fill fields with language models.",
        accent_from="#8b5cf6",
        accent_to="#ec4899",
    ),
    "Integrations": CategoryStyle(
        icon=(
            "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z "
            "M18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M8.6 13.5l6.8 3.9 M15.4 6.6 8.6 10.5"
        ),  # share / connected nodes
        blurb="Bring other apps and the web into Anki.",
        accent_from="#0ea5e9",
        accent_to="#6366f1",
    ),
    "Editing": CategoryStyle(
        icon=(
            "M12 20h9 M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"
        ),  # pencil
        blurb="Keep notes and fields tidy.",
        accent_from="#f59e0b",
        accent_to="#f43f5e",
    ),
}

DEFAULT_CATEGORY_STYLE = CategoryStyle(
    icon="M3 3h7v7H3z M14 3h7v7h-7z M14 14h7v7h-7z M3 14h7v7H3z",  # grid
    blurb="Features in this group.",
    # Theme variables, not literal colours: an unlisted group inherits the active palette
    # instead of guessing one that only works in light or only in dark mode.
    accent_from="var(--accent)",
    accent_to="var(--accent-2)",
)


def category_style(group_name: str) -> CategoryStyle:
    """Return the style for a group, or :data:`DEFAULT_CATEGORY_STYLE` if it is unlisted."""
    return CATEGORY_STYLES.get(group_name, DEFAULT_CATEGORY_STYLE)
