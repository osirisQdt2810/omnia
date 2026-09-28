"""Every stylesheet Omnia ships stays within the QtWebEngine floor.

The floor check used to read ONE sheet — the settings page's — so the Smart Notes page went on
using `color-mix()` four times with nothing to say so. A declaration the engine cannot parse is
dropped whole: below the floor the tool chips kept their border and colour and lost their fill,
and no test could tell, because nothing was wrong with any value.

So the check reads every `*.css` under `src/omnia`, and a sheet that needs an exception has to
be named here with its reason, where a reviewer sees it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "omnia"

#: What the floor lacks. Kept identical to the settings page's own check, which predates this.
FLOOR = ("color-mix(", ":has(", "@container", "@property")

#: Sheet (relative to `src/omnia`) -> features it may use, and why.
#:
#: `typed_accuracy.css` is injected into Anki's own Stats screen, where the card sits in Anki's
#: grid and has to adapt to ITS OWN width (at most 640px), not the window's — which is exactly
#: what a container query is and a media query is not. A faithful replacement needs a JS
#: resize observer toggling width classes: native behaviour on every current engine traded for
#: an approximation, to serve an engine older than Chromium 105. Container queries also degrade
#: gracefully — unmatched, the card keeps its default layout rather than losing a colour.
EXEMPT = {
    "gui/typed_accuracy/web/typed_accuracy.css": {"@container"},
}


def _sheets() -> list[Path]:
    sheets = sorted(SRC.rglob("*.css"))
    assert sheets, f"no stylesheets under {SRC} — the check would pass by reading nothing"
    return sheets


def _without_comments(css: str) -> str:
    """Comments may discuss a feature; only a rule can use one."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.mark.parametrize(
    "sheet", _sheets(), ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_no_stylesheet_uses_a_feature_below_the_floor(sheet):
    rel = sheet.relative_to(SRC).as_posix()
    css = _without_comments(sheet.read_text(encoding="utf-8"))
    allowed = EXEMPT.get(rel, set())

    used = [feature for feature in FLOOR if feature in css and feature not in allowed]

    assert not used, f"{rel} uses {used}, which the QtWebEngine floor does not have"


def test_the_check_covers_the_smart_notes_page():
    """The sheet that slipped through is in the set, not just the one already checked."""
    names = {sheet.relative_to(SRC).as_posix() for sheet in _sheets()}

    assert "gui/smart_notes/web/page.css" in names
    assert "gui/web/settings.css" in names


@pytest.mark.parametrize("rel,features", sorted(EXEMPT.items()))
def test_every_exemption_is_still_needed(rel, features):
    """An exemption outliving its reason becomes a hole: remove the feature, remove the line."""
    css = _without_comments((SRC / rel).read_text(encoding="utf-8"))

    for feature in features:
        assert feature in css, f"{rel} no longer uses {feature}; drop it from EXEMPT"


# --- The tints that replaced `color-mix()` on the Smart Notes page ----------------------------

PAGE = SRC / "gui" / "smart_notes" / "web" / "page.css"

#: Suffix -> the alpha the original `color-mix(... N%, transparent)` gave it.
ALPHA = {"-tint": 0.12, "-wash": 0.10}


def _theme(css: str, name: str) -> dict[str, str]:
    block = re.search(r"body\." + name + r"\s*\{(.*?)\}", css, re.S)
    assert block, f"theme block body.{name} moved"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", _without_comments(block.group(1))))


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@pytest.mark.parametrize("theme", ["omnia-light", "omnia-dark"])
def test_every_tint_is_its_base_colour_at_its_alpha(theme):
    """Mixing by hand is only safe if the mix cannot drift from what it mixes.

    `color-mix(in srgb, X 12%, transparent)` is exactly X at alpha 0.12. Pre-mixed, the tint is
    a second copy of X — so change `--accent` and the chips quietly keep the old hue. This
    re-derives each one from its base, so that change fails here instead.
    """
    tokens = _theme(PAGE.read_text(encoding="utf-8"), theme)
    tints = {name: value for name, value in tokens.items() if name.endswith(tuple(ALPHA))}
    assert len(tints) == 4, f"expected the four hand-mixed tints, found {sorted(tints)}"

    for name, value in tints.items():
        suffix = next(s for s in ALPHA if name.endswith(s))
        base = tokens[name[: -len(suffix)]]
        match = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", value.strip())
        assert match, f"{name} is not an rgba(): {value!r}"
        *channels, alpha = match.groups()

        assert tuple(map(int, channels)) == _rgb(base), f"{name} drifted from {base}"
        assert float(alpha) == ALPHA[suffix], f"{name} alpha {alpha} != {ALPHA[suffix]}"
