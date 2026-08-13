"""Turn a note field's stored markup into plain text.

An Anki field is HTML with Anki's own syntaxes mixed in: ``<b>``/``<div>`` markup, ``[sound:…]``
references, ``<img>`` tags, cloze deletions and HTML entities. Two very different consumers need
the same thing out of it — the words a human wrote:

* the word-lookup panel, which must SHOW the field rather than its markup;
* text-to-speech, which must SPEAK it — a voice reading "strong" aloud because the field
  contained ``<strong>`` is the giveaway that this step was skipped.

Keeping one implementation in ``core`` is deliberate: the two callers live in different plugins,
and ``plugins/*`` must not import each other (see the coupling rule), so a shared seam is the
only place this can live once.
"""

from __future__ import annotations

import re

# Anki's media/markup syntaxes. A media REFERENCE must never survive into text: it is a
# filename, so a voice would happily read "4000B6 plunge dot mp3" out loud.
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
# Anki's inline TTS/AV tags, which are directives rather than content. The CLOSING form
# ([/anki:tts]) must match too, or it survives into the text and gets read aloud.
_AV_TAG_RE = re.compile(r"\[/?anki:[^\]]*\]", re.IGNORECASE)
# {{c1::answer}} / {{c1::answer::hint}} -> the answer (the hint is scaffolding, not content).
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.DOTALL)
_LINE_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")

_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "—",
    "&ndash;": "–",
    "&hellip;": "…",
}


def strip_markup(value: str, *, keep_line_breaks: bool = True) -> str:
    """Return ``value`` as the plain text a person actually wrote.

    Removes tags, media references, Anki AV directives and entities, and unwraps cloze
    deletions to their answer. Block-level breaks become newlines rather than disappearing, so
    a field that lists one item per ``<br>`` stays a list.

    Args:
        value: The raw field value.
        keep_line_breaks: Keep block breaks as newlines. ``False`` flattens everything to one
            line, for a consumer that cannot show them.

    Returns:
        The cleaned text (``""`` for markup that carried no words).
    """
    if not value:
        return ""
    text = _SOUND_RE.sub(" ", value)
    text = _AV_TAG_RE.sub(" ", text)
    text = _IMG_RE.sub(" ", text)
    text = _CLOZE_RE.sub(r"\1", text)
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    return text.replace("\n", " ").strip() if not keep_line_breaks else text
