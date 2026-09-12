"""The ``cloze`` tool: hide a word inside a sentence behind a letter hint, with no AI.

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
* **The SURFACE form is what gets masked**, never the lemma: the hint must match what the
  author wrote, so "survived" becomes ``s_______`` (eight letters), not the lemma's seven.

It no longer emits Anki's ``{{c1::…}}`` markup. That made the field readable only by a cloze
template, and left the answer sitting in the text for anything that unwrapped it —
:func:`omnia.core.lang.text.strip_markup` does exactly that, which is why speaking such a field
needed the ``cloze_audio`` tool. What comes out now is the sentence with the word replaced by a
letter hint: any template renders it, and there is no answer in it to leak.

``cloze_audio`` is unaffected as normally configured: it masks by WORD as well as by marker, so
it should read the ORIGINAL sentence field. Pointed at this tool's output it finds neither the
word nor a marker and says so, which is the right answer — that text has nothing left to hide.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import BaseModel, Field

from omnia.core.config.base import PersistedModel
from omnia.core.lang.text import strip_markup
from omnia.core.lang.word_forms import (
    UNAMBIGUOUS_IRREGULAR,
    Deinflector,
    word_variants,
    words_boundary_pattern,
)
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

#: ``mask`` values — the two shapes the hidden word can take.
#:
#: There is no "show it as-is" mode and no ``{{c1::…}}`` wrapper any more. The tool used to
#: emit Anki cloze markup, which made the field usable only by a cloze template and meant the
#: answer was still sitting in the text for anything that unwrapped it. What it writes now is
#: the sentence with the word itself masked, which any template can render and which carries
#: no answer at all.
MASK_HINT_FIRST = "hint_first"
MASK_HINT_FIRST_LAST = "hint_first_last"
#: What an unrecognised or legacy ``mask`` resolves to — including the old ``"none"``, which no
#: longer has a meaning: leaving the word in place would not hide anything (ADR-010 says a
#: value a older release wrote must degrade, not raise).
MASK_DEFAULT = MASK_HINT_FIRST
MASKS = (MASK_HINT_FIRST, MASK_HINT_FIRST_LAST)

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

# Tags that END a run of text. An INLINE tag is transparent in the projection below, so
# `<b>run</b>ning` reads as one word and `run` does not match a fragment of it. A BLOCK tag has
# to be a barrier instead, or the same transparency glues two separate words together:
# `<div>the cat</div><div>another cat</div>` read as "...the catanother cat...", the boundary
# `\bcat\b` needs vanished, and the first occurrence was silently left unmasked.
#
# The list is HTML's own block-level set plus the flow containers Anki's editor emits, and the
# question to ask of a candidate is only ever "does a browser start a new line at it?" — not
# whether it is common in a note. Anything left out is merely transparent, which is the safe
# default for the tags that matter here (`b`, `i`, `u`, `span`, `ruby`, `font`): a word wrapped
# in one still reads as that word. `option`, `caption` and `legend` are absent for that reason
# — they cannot occur in a field this tool rewrites without the table or form that carries
# them, whose own tags are here.
_BLOCK_TAGS = frozenset(
    [
        "br",
        "div",
        "p",
        "hr",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "aside",
        "nav",
        "header",
        "footer",
        "main",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "form",
        "fieldset",
        "address",
        "details",
        "summary",
    ]
)
_TAG_NAME_RE = re.compile(r"^</?\s*([a-zA-Z][a-zA-Z0-9]*)")


def _is_block_tag(markup: str) -> bool:
    """Whether an HTML tag ends the run of text around it."""
    found = _TAG_NAME_RE.match(markup)
    return bool(found) and found.group(1).lower() in _BLOCK_TAGS


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
    they are consumed instead — an unrecognised ``mask``, and the ``"none"`` an older release
    wrote, both fall back to :data:`MASK_DEFAULT`.
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
    # `separate_cards` is gone. It numbered the c1/c2 cards this tool used to emit, and there
    # are no cards to number now. A value an older release stored rides along as an extra
    # (PersistedModel allows them) and is ignored rather than shown as an option that does
    # nothing — ADR-010 is about not LOSING data, not about keeping dead controls on screen.
    mask: str = Field(
        MASK_DEFAULT,
        description=(
            "How the hidden word is shown: first letter only (s______), or first and last "
            "(s_____e). The underscores match the word's length."
        ),
        # Not a Literal: the runtime stays tolerant of a value a newer release added (see the
        # class docstring), while the picker still renders a dropdown from this schema enum.
        enum=list(MASKS),
    )


class ClozeRewriter:
    """Masks every occurrence of one word in a field value.

    Owns the whole surgery for one (word, mask) pair: which surface forms count as the word,
    where they sit in the ORIGINAL markup, and what each hit becomes. Constructed per run and
    reusable across values, so the caller never has to re-derive the word's forms.
    """

    def __init__(self, word: str, *, mask: str = MASK_DEFAULT) -> None:
        """Build a rewriter for ``word``.

        Args:
            word: The headword to hide (plain text; markup is the caller's problem).
            mask: :data:`MASK_HINT_FIRST` or :data:`MASK_HINT_FIRST_LAST`. Anything else —
                including the ``"none"`` an older release wrote — falls back to
                :data:`MASK_DEFAULT`, because leaving the word in place would hide nothing.
        """
        self._word = word.strip()
        self._mask = mask if mask in MASKS else MASK_DEFAULT

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
        for match_start, match_end, surface in hits:
            pieces.append(value[cursor:match_start])
            pieces.append(self._wrap(surface))
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

        In the projection an INLINE tag is TRANSPARENT (it contributes nothing and joins what
        is on either side of it, so ``<b>run</b>ning`` reads as the single word ``running`` and
        no longer matches ``run``), while an entity, a media reference, an existing cloze — and
        a BLOCK tag — become one separator character, so a match can never read through them.

        The block/inline split is not decoration. With every tag transparent,
        ``<div>the cat</div><div>another cat</div>`` projected to ``...the catanother cat...``:
        the word boundary the pattern needs was gone and the FIRST "cat" was never a candidate,
        so it stayed unmasked with nothing reported. The old round-trip property could not see
        it, because unwrapping a half-wrapped value still gave the input back.

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
            markup = opaque.group(0)
            if not markup.startswith("<") or _is_block_tag(markup):
                # A real barrier: an entity, a media reference, an existing cloze — or a BLOCK
                # tag, which ends the text around it. Nothing may match across one.
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

    def _wrap(self, surface: str) -> str:
        """Replace one matched surface form with its masked shape."""
        return self._hint(surface, self._mask)

    @staticmethod
    def _hint(surface: str, mask: str) -> str:
        """Mask ``surface``, keeping one letter or two.

        ``"survive"`` becomes ``"s______"`` under :data:`MASK_HINT_FIRST` and ``"s_____e"``
        under :data:`MASK_HINT_FIRST_LAST`. The underscore count is the letter count, so the
        length of the word is itself part of the hint.

        Only letters and digits are masked; a space, hyphen or apostrophe is kept. A multi-word
        headword therefore stays visibly multi-word — ``"give up"`` becomes ``"g___ __"``, not
        ``"g______"`` — which the reader needs, and which is also what lets ``cloze_audio``
        recognise each masked word as its own hole.

        A hint never leaves fewer than two letters hidden. A one- or two-letter word is masked
        completely, and a THREE-letter one falls back to the first letter alone even when both
        ends were asked for: ``"cat"`` shown as ``"c_t"`` is not a cloze, it is the answer with
        one letter missing.
        """
        letters = [index for index, char in enumerate(surface) if char.isalnum()]
        both_ends = mask == MASK_HINT_FIRST_LAST and len(letters) >= 4
        if len(letters) <= 2:
            # Mask every letter and keep everything else: a two-letter word gives itself away
            # if either of its letters is shown, whichever mode was asked for.
            keep = set()
        elif both_ends:
            keep = {letters[0], letters[-1]}
        else:
            keep = {letters[0]}
        return "".join(
            char if (not char.isalnum() or index in keep) else "_"
            for index, char in enumerate(surface)
        )


@register_tool("cloze")
class ClozeTool(Tool):
    """Hides a word inside a sentence field behind a letter hint — deterministic, no provider."""

    name: ClassVar[str] = "cloze"
    label: ClassVar[str] = "Cloze"
    description: ClassVar[str] = (
        "Hide the word inside a sentence behind a letter hint — s______ or s_____e, no AI "
        "call. Declines (and lets the next tool try) when the word is not in the sentence."
    )
    kinds: ClassVar[frozenset[str]] = frozenset({"text"})
    deterministic: ClassVar[bool] = True
    # String surgery on the note's own fields — no provider, so a row whose whole chain is this
    # tool has nothing to configure in Provider/Model/Voice.
    uses_provider: ClassVar[bool] = False
    # The two fields the target is DERIVED from — they are what makes this tool's output a
    # function of two other fields, and what the dependency graph draws its edges from. Left
    # blank they resolve to a fallback the picker cannot show, so a user reading the row cannot
    # tell what will be clozed; the runtime keeps the fallbacks for chains synced from older
    # releases, but a chain edited HERE must name both.
    required_params: ClassVar[frozenset[str]] = frozenset(
        {"sentence_field", "word_field"}
    )
    params_model: ClassVar[Optional[type[BaseModel]]] = ClozeParams

    @classmethod
    def reads_prompt(cls, params: Mapping[str, object]) -> bool:
        """Only when ``sentence_field`` is blank, where the prompt IS how the sentence is found.

        With the param set this tool does string surgery on two named fields and never looks at
        the prompt, so a prompt left over from before the chain was configured must not keep
        adding dependency edges (see :meth:`Tool.reads_prompt`).
        """
        return not str(params.get("sentence_field", "") or "").strip()

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
            word, mask=str(params.get("mask", MASK_DEFAULT) or MASK_DEFAULT)
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
