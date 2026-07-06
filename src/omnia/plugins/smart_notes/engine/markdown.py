"""Minimal pure-Python Markdown → HTML converter for generated text fields.

Ported from the reference Smart Notes add-on's ``convert_markdown_to_html`` so generated
text renders as HTML in Anki cards. Deliberately tiny (no third-party Markdown dependency,
which would break the pure-Python / cross-platform vendoring rule): it covers bold, italic,
ATX headers, leading whitespace, and newlines — the subset LLMs actually emit. No Anki
imports, so it unit-tests headless.
"""

from __future__ import annotations

import re

# (pattern, replacement) pairs applied in order. Bold runs before italic so ``**x**`` is not
# eaten by the single-``*`` italic rule. Single-char emphasis follows CommonMark's intraword
# rules so it doesn't corrupt real text: asterisk emphasis requires non-space flanking (so
# ``3 * 4 * 5`` is left alone) but may be intraword; underscore emphasis additionally requires
# word boundaries (so ``read_file_now`` / ``a_b_c`` stay intact — CommonMark forbids intraword
# ``_``). Headers run from the most ``#`` to the fewest so ``###`` isn't matched by ``#`` first.
_INLINE_RULES: tuple[tuple[str, str], ...] = (
    (r"\*\*(.*?)\*\*", r"<strong>\1</strong>"),
    (r"__(.*?)__", r"<strong>\1</strong>"),
    (r"\*(?!\s)(.+?)(?<!\s)\*", r"<em>\1</em>"),
    (r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])", r"<em>\1</em>"),
)
_HEADER_RULES: tuple[tuple[str, str], ...] = (
    (r"###### (.*?)\n", r"<h6>\1</h6>\n"),
    (r"##### (.*?)\n", r"<h5>\1</h5>\n"),
    (r"#### (.*?)\n", r"<h4>\1</h4>\n"),
    (r"### (.*?)\n", r"<h3>\1</h3>\n"),
    (r"## (.*?)\n", r"<h2>\1</h2>\n"),
    (r"# (.*?)\n", r"<h1>\1</h1>\n"),
)
# Leading spaces/tabs only (not newlines), so list/indented lines keep their offset.
_LEADING_WS_RE = re.compile(r"^([ \t]+)", flags=re.MULTILINE)


def convert_markdown_to_html(markdown: str) -> str:
    """Convert a small Markdown subset to HTML for display inside an Anki field.

    Handles bold (``**x**`` / ``__x__``), italic (``*x*`` / ``_x_``), ATX headers
    (``# x`` … ``###### x``), preserves leading whitespace as ``&nbsp;``, and turns newlines
    into ``<br>`` tags. Anything else passes through untouched.

    Args:
        markdown: The raw model text (may already contain HTML, which is left intact).

    Returns:
        The HTML rendering of ``markdown``.
    """
    text = markdown
    for pattern, replacement in _INLINE_RULES:
        text = re.sub(pattern, replacement, text)
    for pattern, replacement in _HEADER_RULES:
        text = re.sub(pattern, replacement, text)
    text = _LEADING_WS_RE.sub(lambda m: "&nbsp;" * len(m.group(1)), text)
    return text.replace("\n", "<br>")
