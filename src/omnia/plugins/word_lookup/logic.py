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
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from omnia.core.text import strip_markup

# Anki's media/markup syntaxes that must never reach the UI as raw text.
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.DOTALL)
# Collapse runs of spaces/tabs but NOT newlines: a field like "Phrasal Verb" separates its
# entries with <br>, and flattening those turned a tidy list into one unreadable blob.
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")
# Block-level markup that ends a visual line in Anki's renderer.
_LINE_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|tr)>", re.IGNORECASE)
# Anki search syntax characters that must be escaped inside a quoted term.
_SEARCH_ESCAPE_RE = re.compile(r'(["\\*_])')
# A field that is only an identifier (a bare number, a UUID, a long hex blob) is bookkeeping,
# never the headword — real note types often put such a field FIRST, which would otherwise make
# the panel's title a UUID. Detected by SHAPE, so no field name is ever hardcoded.
_IDENTIFIER_RE = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})$",
    re.IGNORECASE,
)

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


# Inflection rules, applied to strip a suffix back towards the base form. Each entry is
# (suffix, replacements) and every replacement that leaves a plausible stem is offered as a
# candidate — the search simply ORs them, so an extra wrong guess costs nothing but a miss costs
# the user the card they were looking for.
_DEINFLECT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ies", ("y",)),  # studies -> study
    ("ied", ("y",)),  # studied -> study
    ("ier", ("y",)),  # happier -> happy
    ("iest", ("y",)),  # happiest -> happy
    ("ily", ("y",)),  # happily -> happy
    ("es", ("", "e")),  # goes -> go, boxes -> box
    ("ed", ("", "e")),  # looked -> look, loved -> love
    ("ing", ("", "e")),  # looking -> look, loving -> love
    ("est", ("", "e")),  # tallest -> tall
    ("er", ("", "e")),  # taller -> tall
    ("ly", ("",)),  # quickly -> quick
    ("s", ("",)),  # loves -> love
)
# A stem shorter than this is noise ("as" -> "a"). Two is enough for real bases like "go".
_MIN_STEM = 2
# Only de-inflect words long enough to actually carry a suffix; "as"/"is" must be left alone.
_MIN_INFLECTED = 4
_MAX_VARIANTS = 6


def word_variants(word: str) -> tuple[str, ...]:
    """Return the word plus plausible base forms of it, most-likely first.

    Double-clicking "loved" should still find the card filed under "love". A full lemmatiser
    would need a dictionary and a compiled dependency, neither of which can be vendored into an
    Anki add-on, so this is a small rule-based de-inflector: strip a known suffix, and also undo
    a doubled final consonant (``stopped`` -> ``stop``, ``running`` -> ``run``).

    It is deliberately generous rather than clever — every candidate is OR-ed into one search, so
    a spurious form usually matches nothing, while a missing one loses the user their card.

    Args:
        word: The captured word.

    Returns:
        Deduped candidates including ``word`` itself, capped so the query stays small.
    """
    base = word.strip().lower()
    if not base:
        return ()
    found = [base]

    def offer(candidate: str) -> None:
        if len(candidate) >= _MIN_STEM and candidate not in found:
            found.append(candidate)

    if len(base) < _MIN_INFLECTED:
        return tuple(found)
    for suffix, replacements in _DEINFLECT:
        if not base.endswith(suffix) or len(base) - len(suffix) < _MIN_STEM:
            continue
        stem = base[: -len(suffix)]
        doubled = len(stem) > 2 and stem[-1] == stem[-2] and stem[-1].isalpha()
        for replacement in replacements:
            # "stoppe"/"runne" are impossible words: a doubled consonant means the base LOST an
            # "e" (or never had one), so re-adding it only pads the query with noise.
            if replacement == "e" and doubled:
                continue
            offer(stem + replacement)
        if doubled:
            offer(stem[:-1])  # stopped -> stop, running -> run
        break  # the first matching rule is the most specific one

    return tuple(found[:_MAX_VARIANTS])


def word_boundary_pattern(term: str) -> str:
    """Return a case-insensitive regex matching ``term`` as a WHOLE WORD inside a field.

    This is the middle ground between the two obvious options, both of which are wrong for a
    headword field (measured on a real collection, looking up "port"):

    ===========================  =======  =====================================================
    match                        hits     verdict
    ===========================  =======  =====================================================
    exact (``Word:port``)             5    misses "port of call"
    substring (``Word:*port*``)     146    drags in Deport, Portion, Reporter, important, …
    **word boundary**                 7    Port, port, port of call — what a lookup means
    ===========================  =======  =====================================================

    ``\b`` is only added where it can actually match: it needs a word character beside it, so a
    term like ``c++`` would never match if the boundary were bolted on unconditionally.

    Args:
        term: The raw word being looked up.

    Returns:
        A regex for Anki's ``field:re:`` search.
    """
    return words_boundary_pattern((term,))


def words_boundary_pattern(terms: tuple[str, ...] | list[str]) -> str:
    """Like :func:`word_boundary_pattern` but matching ANY of ``terms``, as one alternation.

    One regex with ``(?:a|b|c)`` keeps the Anki query short no matter how many word forms are
    offered, instead of OR-ing a clause per form per field.
    """
    usable = [t.strip() for t in terms if t and t.strip()]
    if not usable:
        return ""
    # A shared boundary only works if every alternative starts/ends with a word character;
    # otherwise (e.g. "c++") the boundary is dropped so the pattern can still match.
    left = r"\b" if all(t[:1].isalnum() or t[:1] == "_" for t in usable) else ""
    right = r"\b" if all(t[-1:].isalnum() or t[-1:] == "_" for t in usable) else ""
    body = "|".join(re.escape(t) for t in usable)
    grouped = body if len(usable) == 1 else f"(?:{body})"
    return f"(?i){left}{grouped}{right}"


def build_query(
    word: str,
    note_types: list[str] | tuple[str, ...] = (),
    search_fields: dict[str, list[str]] | None = None,
    match_word_forms: bool = True,
) -> str:
    """Build the Anki search string for ``word``, scoped to ``note_types`` and their fields.

    Each note type becomes one OR-ed clause. A note type with configured ``search_fields`` is
    matched only inside those fields, and the term must appear there as a WHOLE WORD
    (``note:"T" ("F1:re:…" OR "F2:re:…")``) — so "port" finds "port" and "port of call" but not
    "important" (see :func:`word_boundary_pattern`). A note type without configured fields is
    matched across all its fields. With no note types at all, the term is searched
    collection-wide.

    Case is not handled here on purpose: Anki folds case itself, so ``LEVEL``/``Level``/``level``
    are already the same search (verified against a real collection).

    Args:
        word: The word/phrase to look up.
        note_types: Note type names to restrict the search to (empty = search everything).
        search_fields: ``{note type: [field, …]}`` limiting where the term may match.
        match_word_forms: Also match plausible base forms ("loved" finds "love").

    Returns:
        An Anki search string, or ``""`` if ``word`` is blank.
    """
    term = escape_search_term(word)
    if not term:
        return ""
    names = [name.strip() for name in note_types if name and name.strip()]
    if not names:
        return f'"{term}"'
    fields_for = search_fields or {}
    clauses = []
    for name in names:
        scope = f'note:"{escape_search_term(name)}"'
        fields = [f.strip() for f in fields_for.get(name, []) if f and f.strip()]
        if fields:
            # WHOLE-WORD match inside the field (see word_boundary_pattern for why neither exact
            # nor substring is right). The clause is quoted so field names with spaces and
            # parentheses — "Word (part of speech)" — parse correctly.
            terms = word_variants(word) if match_word_forms else (word.strip(),)
            pattern = words_boundary_pattern(terms)
            matches = " OR ".join(f'"{field}:re:{pattern}"' for field in fields)
            clauses.append(f"({scope} ({matches}))")
        else:
            clauses.append(f'({scope} "{term}")')
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def strip_html(value: str) -> str:
    """Return ``value`` as plain readable text, KEEPING the author's line breaks.

    A thin alias over the shared :func:`omnia.core.text.strip_markup`. The lookup panel and
    text-to-speech need exactly the same cleaning, and two copies would drift — a fix applied to
    one (media refs, cloze, entities) would silently miss the other.
    """
    return strip_markup(value)


def field_media(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(audio_filenames, image_filenames)`` referenced by a raw field value."""
    if not value:
        return (), ()
    audio = tuple(name.strip() for name in _SOUND_RE.findall(value) if name.strip())
    images = tuple(name.strip() for name in _IMG_RE.findall(value) if name.strip())
    return audio, images


def looks_like_identifier(text: str) -> bool:
    """Whether ``text`` is a bare identifier (number / UUID / hex blob) rather than content."""
    return bool(_IDENTIFIER_RE.match(text.strip()))


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
    word: str = "",
    max_fields: int = 8,
    max_chars: int = 320,
    hidden: tuple[str, ...] = (),
    only: tuple[str, ...] = (),
) -> tuple[str, list[LookupField]]:
    """Choose the title and the fields worth showing, from a note's fields IN NOTE-TYPE ORDER.

    Order is the relevance signal (see the module docstring), with two corrections learned from
    real collections:

    * the title is the field whose text IS the looked-up ``word`` when one exists — a note type
      whose first field is bookkeeping (a "Note ID") would otherwise be titled with a UUID;
    * a field that is only an identifier (bare number, UUID, hex blob) can never become the
      title, detected by shape so no field name is hardcoded.

    Everything else follows the note type's authored order until ``max_fields`` is reached, and
    fields empty after cleaning with no media are dropped — that is what makes a 35-field note
    type readable.

    Args:
        ordered_fields: ``(field_name, raw_value)`` pairs in the note type's own field order.
        word: The looked-up word; when a field's text matches it, that field titles the card.
        max_fields: How many fields to keep after the title.
        max_chars: Per-field truncation budget for long prose.
        hidden: Field names (case-insensitive) the user chose never to show.
        only: An explicit field order for this note type. When given, ONLY these fields are
            shown and in this order — the automatic pick is bypassed entirely (``max_fields``
            no longer applies, because the list is already the user's deliberate choice).

    Returns:
        ``(title, fields)`` — ``title`` is ``""`` when no field had readable text.
    """
    skip = {name.strip().lower() for name in hidden}
    wanted = [name.strip().lower() for name in only if name and name.strip()]
    needle = word.strip().lower()
    kept: list[LookupField] = []
    for name, raw in ordered_fields:
        if name.strip().lower() in skip:
            continue
        audio, images = field_media(raw)
        text = strip_html(raw)
        if not text and not audio and not images:
            continue  # empty after cleaning and no media: nothing to show
        kept.append(
            LookupField(
                name=name,
                text=text[:max_chars],
                kind=_classify(text, audio, images),
                audio=audio,
                images=images,
            )
        )

    # Title: the field that IS the looked-up word, else the first field carrying real content.
    title_index = -1
    if needle:
        for index, item in enumerate(kept):
            if item.text.strip().lower() == needle:
                title_index = index
                break
    if title_index < 0:
        for index, item in enumerate(kept):
            if item.text and not looks_like_identifier(item.text):
                title_index = index
                break
    if title_index < 0:
        return "", kept[:max_fields]
    title = kept[title_index].text
    rest = [item for index, item in enumerate(kept) if index != title_index]
    if wanted:
        # The explicit list controls the FIELD LIST, not the title — the title is the word
        # itself, already chosen above from every field. Honour the user's order, and never cap
        # it: the list is their deliberate choice, so truncating it would drop what they asked
        # for.
        by_name = {item.name.strip().lower(): item for item in rest}
        return title, [by_name[key] for key in wanted if key in by_name]
    # Identifier-only fields are bookkeeping noise, but the rule is ORDERING, not visibility:
    # they sink to the bottom rather than disappearing, so nothing is ever silently withheld.
    rest.sort(key=lambda item: looks_like_identifier(item.text))
    return title, rest[:max_fields]


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
        # A note whose TITLE is bookkeeping can still be the right card, so an exact match in
        # any single field ranks just behind an exact title match — this is what puts the real
        # "plunge" note above a "surf" note that merely mentions plunge in its synonyms.
        if any(f.text.strip().lower() == needle for f in card.fields):
            return 1
        if title.startswith(needle):
            return 2
        if needle in title:
            return 3
        return 4

    return [
        card for _, card in sorted(enumerate(cards), key=lambda p: (score(p[1]), p[0]))
    ]
