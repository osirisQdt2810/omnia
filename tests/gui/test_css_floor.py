"""Every piece of CSS Omnia ships stays within the QtWebEngine floor.

The floor check used to read ONE sheet — the settings page's — so the Smart Notes page went on
using `color-mix()` four times with nothing to say so. A declaration the engine cannot parse is
dropped whole: below the floor the tool chips kept their border and colour and lost their fill,
and no test could tell, because nothing was wrong with any value.

CSS does not only live in `.css` files, so neither does this check. It reads every stylesheet,
every `<style>` block in a page template, every string literal in the Python that emits HTML,
and the page scripts that set styles. A source that needs an exception is named in `EXEMPT` with
its reason, where a reviewer sees it — and this is the ONE place the feature list lives.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "omnia"

#: What the floor lacks. Kept here only: a second copy of this list was how the check came to
#: read one sheet while the rest drifted.
FLOOR = ("color-mix(", ":has(", "@container", "@property")

#: Source (relative to `src/omnia`) -> features it may use, and why.
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


def _without_css_comments(text: str) -> str:
    """Comments may discuss a feature; only a rule can use one."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _style_blocks(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S | re.I))


def _python_strings(source: str) -> str:
    """Every string literal EXCEPT docstrings.

    Scanning the file itself would trip on `@property` — a Python decorator on every other
    class — and on prose. A literal is what reaches a page; a docstring is not.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstrings.add(id(value))
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


def _without_js_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", _without_css_comments(source))


#: Kind -> (glob, how to pull the CSS out of such a file).
KINDS = {
    "css": ("*.css", _without_css_comments),
    "html": ("*.html", lambda text: _without_css_comments(_style_blocks(text))),
    "py": ("*.py", _python_strings),
    "js": ("*.js", _without_js_comments),
}


def _sources(kind: str) -> dict[str, str]:
    pattern, extract = KINDS[kind]
    return {
        path.relative_to(SRC).as_posix(): extract(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob(pattern))
    }


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_no_shipped_css_uses_a_feature_below_the_floor(kind):
    violations = [
        f"{rel}: {feature}"
        for rel, css in _sources(kind).items()
        for feature in FLOOR
        if feature in css and feature not in EXEMPT.get(rel, set())
    ]

    assert not violations, "below the QtWebEngine floor:\n" + "\n".join(violations)


def test_the_scan_reads_what_it_claims_to():
    """A check that silently reads nothing passes; this is the part that cannot."""
    sheets = _sources("css")
    pages = _sources("html")

    assert "gui/smart_notes/web/page.css" in sheets, "the sheet that slipped through"
    assert "gui/web/settings.css" in sheets
    assert any("<style" in (SRC / rel).read_text(encoding="utf-8") for rel in pages)
    assert len(_sources("py")) > 50, "the Python scan found almost nothing to read"


def test_a_python_decorator_is_not_mistaken_for_css():
    """`@property` is a decorator in Python; only a string literal is a stylesheet."""
    code = 'class A:\n    """Mentions color-mix( in prose."""\n    @property\n    def x(self): ...\n'

    assert _python_strings(code) == ""
    assert "@property" in _python_strings("CSS = \"@property --x { syntax: '*'; }\"")


@pytest.mark.parametrize("rel,features", sorted(EXEMPT.items()))
def test_every_exemption_is_still_needed(rel, features):
    """An exemption outliving its reason becomes a hole: remove the feature, remove the line."""
    css = _without_css_comments((SRC / rel).read_text(encoding="utf-8"))

    for feature in features:
        assert feature in css, f"{rel} no longer uses {feature}; drop it from EXEMPT"


# --- The tints that replaced `color-mix()` on the Smart Notes page ----------------------------

PAGE = SRC / "gui" / "smart_notes" / "web" / "page.css"

#: Suffix -> the alpha the original `color-mix(... N%, transparent)` gave it.
ALPHA = {"-tint": 0.12, "-wash": 0.10}


def _themes() -> list[str]:
    """Read from the sheet, so a third theme is checked the day it is added."""
    names = re.findall(r"body\.(omnia-[\w-]+)\s*\{", PAGE.read_text(encoding="utf-8"))
    assert {"omnia-light", "omnia-dark"} <= set(names), f"theme blocks moved: {names}"
    return names


def _theme(css: str, name: str) -> dict[str, str]:
    block = re.search(r"body\." + name + r"\s*\{(.*?)\}", css, re.S)
    assert block, f"theme block body.{name} moved"
    body = _without_css_comments(block.group(1))
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@pytest.mark.parametrize("theme", _themes())
def test_every_tint_is_its_base_colour_at_its_alpha(theme):
    """Mixing by hand is only safe if the mix cannot drift from what it mixes.

    `color-mix(in srgb, X 12%, transparent)` is exactly X at alpha 0.12. Pre-mixed, the tint is
    a second copy of X — so change `--accent` and the chips quietly keep the old hue. This
    re-derives each one from its base, so that change fails here instead.
    """
    tokens = _theme(PAGE.read_text(encoding="utf-8"), theme)
    suffixes = tuple(ALPHA)
    tints = {name: value for name, value in tokens.items() if name.endswith(suffixes)}
    assert len(tints) == 4, f"expected the four hand-mixed tints, found {sorted(tints)}"

    for name, value in tints.items():
        suffix = next(s for s in ALPHA if name.endswith(s))
        base = tokens[name[: -len(suffix)]]
        rgba = r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)"
        match = re.fullmatch(rgba, value.strip())
        assert match, f"{name} is not an rgba(): {value!r}"
        *channels, alpha = match.groups()

        assert tuple(map(int, channels)) == _rgb(base), f"{name} drifted from {base}"
        assert float(alpha) == ALPHA[suffix], f"{name} alpha {alpha} != {ALPHA[suffix]}"
