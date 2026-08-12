"""Pure lookup logic: build the search, clean field HTML, and triage which fields to show.

No ``aqt``/``anki`` imports — every function here is a plain transformation over data the glue
layer read from the collection, so the whole ranking/triage behaviour unit-tests headless.

The hard problem this module solves is **field triage**. A real vocabulary note type can carry
~35 fields (``Word``, ``Word (ipa)``, ``Definition``, ``Meaning (vi)``, ``Example 1``,
``Example 1 (audio)``, ``Synonyms``, ``Image``, …), most of them empty on any given note, with
values that are raw HTML containing ``[sound:x.mp3]``, ``<img src="y.jpg">`` and cloze markup.
Dumping every field is unreadable, and hardcoding field names would only ever fit one user's
collection. So the rules here are generic and ordering-driven:

* a field is a candidate only if it has content AFTER cleaning (or is pure media);
* the note type's own field ORDER is the relevance signal — Anki authors put the headword and
  the meaning first — so the first non-empty field becomes the title and the rest follow in
  order, capped;
* fields that are only media become compact badges rather than walls of markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field

# Anki's media/markup syntaxes that must never reach the UI as raw text.
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
# Anki search syntax characters that must be escaped inside a quoted term.
_SEARCH_ESCAPE_RE = re.compile(r'(["\\*_])')

_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}

# Field kinds the UI renders differently.
KIND_TEXT = "text"
KIND_AUDIO = "audio"
KIND_IMAGE = "image"

# Anki card.type -> a human state name (mirrors display_interval's mapping).
_CARD_STATES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}


@dataclass(frozen=True)
class LookupField:
    """One field worth showing: its label, cleaned text, and any media it referenced."""

    name: str
    text: str
    kind: str = KIND_TEXT
    audio: tuple[str, ...] = ()
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class LookupCard:
    """A search hit, flattened into exactly what a UI needs to render it."""

    note_id: int
    note_type: str
    deck: str
    title: str
    fields: tuple[LookupField, ...] = ()
    tags: tuple[str, ...] = ()
    state: str = "new"
    interval_days: int = 0
    reps: int = 0
    lapses: int = 0


@dataclass
class LookupResult:
    """The whole answer to one lookup: the hits plus why there might be none."""

    word: str
    cards: list[LookupCard] = dataclass_field(default_factory=list)
    truncated: bool = False

    @property
    def found(self) -> bool:
        """Whether anything matched."""
        return bool(self.cards)


def escape_search_term(term: str) -> str:
    """Escape ``term`` for use inside a quoted Anki search phrase.

    Anki treats ``"``, ``*``, ``_`` and ``\\`` as syntax; a word containing them would either
    break the query or silently widen it (``_`` is a single-character wildcard).

    Args:
        term: The raw user text to search for.

    Returns:
        The escaped term, safe to wrap in double quotes.
    """
    return _SEARCH_ESCAPE_RE.sub(r"\\\1", term.strip())


def build_query(word: str, note_types: list[str] | tuple[str, ...] = ()) -> str:
    """Build the Anki search string for ``word``, optionally restricted to ``note_types``.

    The term is searched across all fields (field names differ per collection, so targeting one
    would not generalise). When note types are given they are OR-ed into a parenthesised filter
    so the search stays scoped to what the user marked as searchable.

    Args:
        word: The word/phrase to look up.
        note_types: Note type names to restrict the search to (empty = search everything).

    Returns:
        An Anki search string, or ``""`` if ``word`` is blank.
    """
    term = escape_search_term(word)
    if not term:
        return ""
    query = f'"{term}"'
    names = [name for name in note_types if name and name.strip()]
    if names:
        joined = " OR ".join(f'note:"{escape_search_term(name)}"' for name in names)
        query = f"({joined}) {query}"
    return query


def strip_html(value: str) -> str:
    """Return ``value`` as plain readable text (tags, media refs, cloze and entities removed)."""
    if not value:
        return ""
    text = _SOUND_RE.sub(" ", value)
    text = _IMG_RE.sub(" ", text)
    text = _CLOZE_RE.sub(r"\1", text)  # show the cloze answer, not the markup
    text = re.sub(r"<br\s*/?>|</(p|div|li)>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    return _WHITESPACE_RE.sub(" ", text).strip()


def field_media(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(audio_filenames, image_filenames)`` referenced by a raw field value."""
    if not value:
        return (), ()
    audio = tuple(name.strip() for name in _SOUND_RE.findall(value) if name.strip())
    images = tuple(name.strip() for name in _IMG_RE.findall(value) if name.strip())
    return audio, images


def _classify(text: str, audio: tuple[str, ...], images: tuple[str, ...]) -> str:
    """Pick the render kind for a field from what survived cleaning."""
    if text:
        return KIND_TEXT
    if images:
        return KIND_IMAGE
    if audio:
        return KIND_AUDIO
    return KIND_TEXT


def triage_fields(
    ordered_fields: list[tuple[str, str]],
    *,
    max_fields: int = 8,
    max_chars: int = 320,
    hidden: tuple[str, ...] = (),
) -> tuple[str, list[LookupField]]:
    """Choose the title and the fields worth showing, from a note's fields IN NOTE-TYPE ORDER.

    Order is the relevance signal (see the module docstring): the first field with readable text
    becomes the title, and the remaining non-empty fields follow in their authored order until
    ``max_fields`` is reached. Fields that are empty after cleaning AND carry no media are
    dropped entirely — that is what makes a 35-field note type readable.

    Args:
        ordered_fields: ``(field_name, raw_value)`` pairs in the note type's own field order.
        max_fields: How many fields to keep after the title.
        max_chars: Per-field truncation budget for long prose.
        hidden: Field names (case-insensitive) the user chose never to show.

    Returns:
        ``(title, fields)`` — ``title`` is ``""`` when no field had readable text.
    """
    skip = {name.strip().lower() for name in hidden}
    title = ""
    chosen: list[LookupField] = []
    for name, raw in ordered_fields:
        if name.strip().lower() in skip:
            continue
        audio, images = field_media(raw)
        text = strip_html(raw)
        if not text and not audio and not images:
            continue  # empty after cleaning and no media: nothing to show
        if not title and text:
            title = text[:max_chars]
            continue  # the headword is rendered as the title, not repeated as a field
        if len(chosen) >= max_fields:
            break
        chosen.append(
            LookupField(
                name=name,
                text=text[:max_chars],
                kind=_classify(text, audio, images),
                audio=audio,
                images=images,
            )
        )
    return title, chosen


def card_state(card_type: int) -> str:
    """Map Anki's ``card.type`` to a display state name."""
    return _CARD_STATES.get(int(card_type), "review")


def rank_cards(cards: list[LookupCard], word: str) -> list[LookupCard]:
    """Order hits so the most likely intended card comes first.

    An exact title match beats a prefix match, which beats a mere substring hit; ties keep their
    original (Anki search) order. Without this a search for "plunge" can surface "plunger" or a
    long example sentence above the actual "plunge" note.

    Args:
        cards: The hits to order.
        word: The looked-up word.

    Returns:
        A new, ranked list.
    """
    needle = word.strip().lower()

    def score(card: LookupCard) -> int:
        title = card.title.strip().lower()
        if title == needle:
            return 0
        if title.startswith(needle):
            return 1
        if needle in title:
            return 2
        return 3

    return [card for _, card in sorted(enumerate(cards), key=lambda p: (score(p[1]), p[0]))]
