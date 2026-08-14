"""The ``cloze`` tool: turn a sentence into a cloze deletion around a word, with no AI.

The first deterministic builtin, and the reason the whole tool seam exists: a field configured
``[cloze, ai]`` costs nothing when the word really is in the sentence, and only falls through to
the paid provider when it is not (:class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`).

Three details carry the tool's whole value, and each is a documented decision below:

* **Inflection works in BOTH directions.** ``core.lang.word_forms`` de-inflects
  INFLECTED → base ("survived" → "survive"), so a headword field holding the base form finds
  nothing in a sentence that inflects it — the common case here, since the base field usually
  holds the lemma. :class:`ClozeRewriter` therefore also de-inflects the SENTENCE's own tokens
  and keeps the ones that share a base form with the word.
* **Matching sees text; rewriting edits the ORIGINAL value.** The scan runs over the field's
  plain-text spans only (see :func:`_plain_spans`), so a match can never start inside an HTML
  tag, a ``[sound:…]`` reference or an existing cloze — and the value that comes back is the
  original markup with only the matched surfaces wrapped.
* **The SURFACE form is what gets wrapped**, never the lemma: the card must read as the author
  wrote it, so "survived" stays "survived" inside ``{{c1::…}}``.

The emitted ``{{c1::…}}`` is inert everywhere else in the engine: the prompt interpolator's
field-ref regex skips a cloze opener and :func:`omnia.core.text.strip_markup` unwraps one to its
answer — which is exactly why speaking such a field needs the later ``cloze_audio`` tool rather
than plain TTS.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import BaseModel, Field

from omnia.core.config.base import PersistedModel
from omnia.core.lang.word_forms import (
    UNAMBIGUOUS_IRREGULAR,
    Deinflector,
    word_variants,
    words_boundary_pattern,
)
from omnia.core.text import strip_markup
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.rules import rule_source_fields
from omnia.plugins.smart_notes.engine.tools.base import (
    NotApplicable,
    Produced,
    Tool,
    ToolOutcome,
)
from omnia.plugins.smart_notes.engine.tools.registry import register_tool

if TYPE_CHECKING:
    from collections.abc import Mapping

    from omnia.plugins.smart_notes.config import SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.tools.base import ToolContext, ToolRequest

#: ``mask`` values: wrap the surface as-is, or add a first/last-letter hint.
MASK_NONE = "none"
MASK_HINT_FIRST_LAST = "hint_first_last"

# What the matcher must treat as OPAQUE: a match may never start, end, or run inside one of
# these. HTML tags and Anki's media/AV references are not words; an HTML entity would otherwise
# read as the word "nbsp"; and an EXISTING cloze must be left alone rather than nested inside a
# new one (re-running the tool on an already-clozed field is then a no-op for those spans).
_OPAQUE_RE = re.compile(
    r"<[^>]*>"
    r"|\[sound:[^\]]*\]"
    r"|\[/?anki:[^\]]*\]"
    r"|&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);"
    r"|\{\{c\d+::.*?\}\}",
    re.IGNORECASE | re.DOTALL,
)

# The probe built from the HEADWORD drops the irregular forms whose base is a different word
# ("left" -> "leave", "rose" -> "rise"): resolving those would hide an unrelated word, and this
# tool writes its output back to the note. The SENTENCE's tokens keep the full table, so a
# "leave" card still hides the "left" in its example — the safe direction.
_HEADWORD_DEINFLECTOR = Deinflector(irregular=UNAMBIGUOUS_IRREGULAR)

# Word-ish runs used to harvest the sentence's own tokens for the inverse de-inflection.
_TOKEN_RE = re.compile(r"\w+")

# Function words that must never be clozed just because they happen to be a SPECULATIVE stem of
# the headword. Stripping "es" turns "toes" into "to" and "ones" into "on" — legitimate guesses
# for widening an Anki search, ruinous when compiled into a rewrite: the tool would hide every
# "to" in the sentence and stop the chain, so `ai` never gets to correct it.
#
# Length alone cannot separate these ("goes" -> "go" is the same shape as "toes" -> "to"), so
# the distinction has to be lexical. The list is deliberately tiny — only words no vocabulary
# deck studies on their own — and it never filters the headword itself: if the user's word field
# really is "to", "to" is still clozed.
_FUNCTION_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "he",
        "she",
        "we",
        "they",
        "you",
        "i",
        "me",
        "my",
        "his",
        "her",
        "our",
        "your",
        "their",
        "them",
        "us",
        "him",
        "do",
        "does",
        "did",
        "so",
        "no",
        "not",
        "up",
        "out",
        "off",
        "over",
        "under",
        "then",
        "than",
        "there",
        "here",
        "when",
        "where",
        "who",
        "whom",
        "which",
    ]
)


def _plain_spans(value: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` offsets of ``value``'s plain-text runs, in order.

    The complement of :data:`_OPAQUE_RE`: everything that is NOT markup, a media reference, an
    entity or an existing cloze. Matching span-by-span (rather than on a stripped copy plus an
    offset map) is what guarantees both halves of the contract at once — offsets are already the
    original value's, and a match can never span markup because the spans stop at it.

    Args:
        value: The raw field value.

    Returns:
        Non-empty ``(start, end)`` pairs into ``value``.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _OPAQUE_RE.finditer(value):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(value):
        spans.append((cursor, len(value)))
    return spans


class ClozeParams(PersistedModel):
    """The ``cloze`` tool's per-field options.

    A :class:`~omnia.core.config.base.PersistedModel` (not a strict one) because these live
    inside the field row's persisted chain and therefore sync between devices on different
    Omnia releases: an option a NEWER release added must not turn this tool into an error
    attempt on an older one (ADR-010). Unknown values of a KNOWN option are neutralised where
    they are consumed instead — an unrecognised ``mask`` simply wraps without a hint.
    """

    sentence_field: str = Field(
        "",
        description=(
            "Field holding the sentence to cloze. Blank = the field's first prompt "
            "reference, else the note type's base field."
        ),
    )
    word_field: str = Field(
        "",
        description="Field holding the word to hide. Blank = the note type's base field.",
    )
    match_word_forms: bool = Field(
        True,
        description=(
            "Also match inflected forms of the word (run/running, survive/survived)."
        ),
    )
    separate_cards: bool = Field(
        False,
        description=(
            "Give each occurrence its own card (c1, c2, …) instead of hiding them all "
            "on c1."
        ),
    )
    mask: str = Field(
        MASK_NONE,
        description="Show a first/last-letter hint (s___e) in place of the hidden word.",
        # Not a Literal: the runtime stays tolerant of a value a newer release added (see the
        # class docstring), while the picker still renders a dropdown from this schema enum.
        enum=[MASK_NONE, MASK_HINT_FIRST_LAST],
    )


class ClozeRewriter:
    """Wraps every occurrence of one word in a field value as an Anki cloze deletion.

    Owns the whole surgery for one (word, options) pair: which surface forms count as the word,
    where they sit in the ORIGINAL markup, and how each hit is wrapped. Constructed per run and
    reusable across values, so the caller never has to re-derive the word's forms.
    """

    def __init__(
        self,
        word: str,
        *,
        match_word_forms: bool = True,
        separate_cards: bool = False,
        mask: str = MASK_NONE,
    ) -> None:
        """Build a rewriter for ``word``.

        Args:
            word: The headword to hide (plain text; markup is the caller's problem).
            match_word_forms: Also hide inflected forms of the word.
            separate_cards: Number each occurrence (c1, c2, …) instead of reusing c1.
            mask: :data:`MASK_HINT_FIRST_LAST` to emit a ``s___e`` hint; anything else wraps
                the surface with no hint.
        """
        self._word = word.strip()
        self._match_word_forms = match_word_forms
        self._separate_cards = separate_cards
        self._mask = mask

    def occurrences(self, value: str) -> list[tuple[int, int, str]]:
        """Return each occurrence of the word in ``value`` as ``(start, end, surface)``.

        Offsets index the ORIGINAL value, markup included, so a caller can cut it up without
        re-deriving anything. Public because :mod:`~omnia.plugins.smart_notes.engine.tools.cloze_audio`
        needs the same spans for a different surgery (replacing them with silence instead of
        wrapping them): the matcher here handles markup projection, the two-way de-inflection
        and the function-word filter, and a second copy of that would drift from this one.

        Args:
            value: The raw sentence field value (markup included).

        Returns:
            The hits in document order; empty when the word does not occur.
        """
        pattern = self._pattern(value, _plain_spans(value))
        if pattern is None:
            return []
        return self._matches(value, pattern)

    def rewrite(self, value: str) -> Optional[str]:
        """Return ``value`` with every occurrence of the word clozed, or None when it has none.

        Args:
            value: The raw sentence field value (markup included).

        Returns:
            The rewritten value, or ``None`` when nothing matched (the caller turns that into a
            :class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`, so a chain can
            fall through to the next tool).
        """
        hits = self.occurrences(value)
        if not hits:
            return None
        pieces: list[str] = []
        cursor = 0
        for occurrence, (match_start, match_end, surface) in enumerate(hits, start=1):
            pieces.append(value[cursor:match_start])
            pieces.append(self._wrap(surface, occurrence))
            cursor = match_end
        pieces.append(value[cursor:])
        return "".join(pieces)

    def _matches(
        self, value: str, pattern: re.Pattern[str]
    ) -> list[tuple[int, int, str]]:
        """Return each whole-word hit as ``(start, end, surface)`` offsets into ``value``.

        Matching happens on a PROJECTION of the value rather than on its raw spans, because
        ``finditer(value, start, end)`` treats ``end`` as a truncation — re behaves "as if the
        string is end characters long" — so ``\\b`` matches at a span's right edge even when a
        word character sits just past it behind a tag. ``She was <b>run</b>ning fast.`` would
        then cloze the fragment ``run``.

        In the projection an HTML tag is TRANSPARENT (it contributes nothing and joins what is
        on either side of it, so ``<b>run</b>ning`` reads as the single word ``running`` and no
        longer matches ``run``), while an entity, a media reference and an existing cloze become
        one separator character, so a match can never read through them.

        A hit is then rejected if its original range is not contiguous — i.e. the match reads
        across a tag. That keeps ``sur<b>vived</b>`` declining, as its own test requires, while
        the rewrite still edits the ORIGINAL markup.
        """
        plain_chars: list[str] = []
        offsets: list[int] = []
        cursor = 0
        for opaque in _OPAQUE_RE.finditer(value):
            for index in range(cursor, opaque.start()):
                plain_chars.append(value[index])
                offsets.append(index)
            if not opaque.group(0).startswith("<"):
                # Not a tag: a real barrier, so nothing may match across it.
                plain_chars.append("\x00")
                offsets.append(opaque.start())
            cursor = opaque.end()
        for index in range(cursor, len(value)):
            plain_chars.append(value[index])
            offsets.append(index)

        hits: list[tuple[int, int, str]] = []
        plain = "".join(plain_chars)
        for match in pattern.finditer(plain):
            start, end = match.start(), match.end()
            first, last = offsets[start], offsets[end - 1]
            # Contiguous in the original == the match did not read through a tag.
            if last - first != end - start - 1:
                continue
            hits.append((first, last + 1, value[first : last + 1]))
        return hits

    def _pattern(
        self, value: str, spans: list[tuple[int, int]]
    ) -> Optional[re.Pattern[str]]:
        """Compile the whole-word alternation to search for, or None when there is nothing to."""
        terms = self._surface_terms(value, spans)
        source = words_boundary_pattern(terms)
        return re.compile(source) if source else None

    def _surface_terms(self, value: str, spans: list[tuple[int, int]]) -> list[str]:
        """Return every spelling that counts as the word, longest first.

        Two directions, because the de-inflector only walks one way (INFLECTED → base):

        * the word's own variants, which catch a base form in the sentence when the FIELD holds
          an inflected one ("survived" in the field, "survive" in the sentence);
        * the sentence's tokens whose variants meet the word's, which catch the far more common
          opposite case ("survive" in the field, "survived" in the sentence).

        Longest-first ordering keeps the alternation from settling for a shorter form when a
        longer one starts at the same place.

        The word's own variants are used ONLY as a probe, never as match terms. ``word_variants``
        is deliberately generous because a spurious candidate costs nothing in an Anki search —
        but here a candidate is compiled straight into the cloze regex, where a stem that happens
        to be a real word rewrites the note. ``toes`` yields the stem ``to``, which would hide
        every "to" in the sentence; ``bees``/``ones``/``uses`` do the same with ``be``/``on``/
        ``us``. The token loop below already covers the direction those stems were meant to
        serve: a sentence token counts when ITS variants meet the word's.
        """
        primary = self._word.lower()
        if not self._match_word_forms:
            return [primary] if primary else []
        targets = set(_HEADWORD_DEINFLECTOR.variants(self._word))
        terms = {primary}
        for start, end in spans:
            for token in _TOKEN_RE.findall(value[start:end]):
                lowered = token.lower()
                if lowered in terms:
                    continue
                # The two must meet on a base that carries meaning. Without this the probe's
                # own speculative stems come back in through the token: "toes" offers the stem
                # "to", the sentence's "to" shares it, and every "to" gets clozed. The headword
                # itself is always a valid meeting point, however common it is.
                shared = targets.intersection(word_variants(token))
                if any(
                    base == primary or base not in _FUNCTION_WORDS for base in shared
                ):
                    terms.add(lowered)
        return sorted((term for term in terms if term), key=lambda t: (-len(t), t))

    def _wrap(self, surface: str, occurrence: int) -> str:
        """Wrap one matched surface form as ``{{cN::surface}}`` (plus a hint when masking)."""
        index = occurrence if self._separate_cards else 1
        body = surface
        if self._mask == MASK_HINT_FIRST_LAST:
            body = f"{surface}::{self._hint(surface)}"
        return "{{c" + str(index) + "::" + body + "}}"

    @staticmethod
    def _hint(surface: str) -> str:
        """Return the first/last-letter hint for ``surface`` (``"survive"`` → ``"s______e"``).

        A one- or two-letter word is masked completely: showing both of its letters would give
        the answer away, which is the one thing the hint must not do.
        """
        if len(surface) <= 2:
            return "_" * len(surface)
        return surface[0] + "_" * (len(surface) - 2) + surface[-1]


@register_tool("cloze")
class ClozeTool(Tool):
    """Hides a word inside a sentence field as a cloze deletion — deterministic, no provider."""

    name: ClassVar[str] = "cloze"
    label: ClassVar[str] = "Cloze"
    description: ClassVar[str] = (
        "Wrap the word in {{c1::…}} inside a sentence field — no AI call. Declines "
        "(and lets the next tool try) when the word is not in the sentence."
    )
    kinds: ClassVar[frozenset[str]] = frozenset({"text"})
    deterministic: ClassVar[bool] = True
    params_model: ClassVar[Optional[type[BaseModel]]] = ClozeParams

    @classmethod
    def referenced_fields(cls, params: Mapping[str, object]) -> list[str]:
        """Return the fields the params NAME, so they become real dependency edges.

        Only the explicitly configured names: the defaults resolve to the rule's own prompt
        refs (already prerequisites) or to the note type's base field (always present), so
        neither adds an edge.
        """
        names = [
            str(params.get("sentence_field", "") or "").strip(),
            str(params.get("word_field", "") or "").strip(),
        ]
        return [name for name in names if name]

    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Cloze the sentence field for this rule's note, or decline.

        Declines (:class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`) when the
        sentence or the word is blank, or when the word — in any of its forms — does not occur
        in the sentence. Both are the fall-through the ``[cloze, ai]`` chain is built on, so the
        reason names the word and the field it looked in.
        """
        rule = request.rule
        params = request.params
        sentence_field = str(params.get("sentence_field", "") or "").strip() or (
            default_source_field(rule)
        )
        word_field = str(params.get("word_field", "") or "").strip() or (
            default_word_field(rule)
        )
        sentence = field_value(request.fields, sentence_field)
        word = strip_markup(
            field_value(request.fields, word_field), keep_line_breaks=False
        ).strip()
        if not strip_markup(sentence).strip():
            return NotApplicable(
                f"nothing to cloze — {sentence_field or 'the sentence field'} is empty"
            )
        if not word:
            return NotApplicable(
                f"nothing to cloze — {word_field or 'the word field'} is empty"
            )
        if sentence_field.strip().lower() == word_field.strip().lower():
            # Both default independently — sentence_field to the rule's first prompt ref and
            # word_field to the note type's base field — so a rule whose only ref IS the base
            # field lands on the same field twice. Clozing a word inside itself just yields
            # "{{c1::word}}", which is not a card; decline so the chain can fall through.
            return NotApplicable(
                f"the sentence and the word would both come from {sentence_field!r}"
            )
        clozed = ClozeRewriter(
            word,
            match_word_forms=bool(params.get("match_word_forms", True)),
            separate_cards=bool(params.get("separate_cards", False)),
            mask=str(params.get("mask", MASK_NONE) or MASK_NONE),
        ).rewrite(sentence)
        if clozed is None:
            return NotApplicable(
                f"{word!r} (and its word forms) is not in {sentence_field!r}"
            )
        return Produced(GenerationResult("text", text=clozed))


def default_source_field(rule: SmartNotesFieldRule) -> str:
    """The field a cloze-ish tool reads when its param is blank: first prompt ref, else the base.

    Reads the rule's derived sources through
    :func:`~omnia.plugins.smart_notes.engine.rules.rule_source_fields` — the same "what does
    this field read" helper the graph and ordering use — so the default can never point
    somewhere the dependency graph does not already know about. Shared with ``cloze_audio``,
    whose ``source_field`` must default identically or the pair would read different fields on
    the same note.
    """
    sources = rule_source_fields(rule)
    return sources[0] if sources else (rule.base_field or rule.source_field)


def default_word_field(rule: SmartNotesFieldRule) -> str:
    """The field holding the word to hide when the param is blank: the note type's base field."""
    return rule.base_field or rule.source_field


def field_value(fields: Mapping[str, str], name: str) -> str:
    """Return ``fields[name]``, matching case-insensitively (Anki field names are).

    Mirrors how the service's block gate compares prerequisite names, so a chain configured
    with "sentence" finds the note's "Sentence" instead of silently declining.
    """
    if not name:
        return ""
    if name in fields:
        return str(fields[name])
    lowered = name.strip().lower()
    for key, value in fields.items():
        if key.strip().lower() == lowered:
            return str(value)
    return ""
