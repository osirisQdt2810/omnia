"""Word-form helpers: rule-based de-inflection and whole-word regex building.

Promoted here from ``plugins/word_lookup/logic.py`` because more than one feature needs the
same answer to "what other spellings of this word count as the same word?": word-lookup
widens its collection search with them, and smart-notes' cloze tool must find the headword
inside an example sentence even when the sentence inflects it ("run" vs "running").

Pure stdlib and free of ``aqt``/``anki`` imports, so it unit-tests headless. Everything here
is deliberately English-rule-based and generous rather than clever: a spurious candidate
usually matches nothing, while a missing one loses the user the card they were after.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
# Irregular forms, which no suffix rule can reach: "ran" shares no ending with "run". English
# irregulars are a CLOSED set, so unlike the rules above this table is knowledge, not guesswork —
# it is consulted first and its answers are exact.
#
# Two properties matter for how it is used:
#   * A form may map to SEVERAL bases when English is genuinely ambiguous ("left" is both the
#     past of "leave" and a direction; "saw" is both the past of "see" and a tool). Every base
#     is offered, and the word itself is always kept, so an ambiguous form widens the search
#     rather than redirecting it.
#   * Only INFLECTED -> BASE is stored. The reverse is not needed: callers that must match a
#     lemma against a sentence de-inflect the sentence's tokens too and compare bases, so
#     "run" (base field) and "ran" (sentence) meet at "run".
#
# Short forms like "ran"/"ate"/"men" are why the lookup runs BEFORE the ``min_inflected``
# guard — that guard exists to stop the suffix rules mangling short words, and an exact table
# needs no such protection.
_IRREGULAR_FORMS: dict[str, tuple[str, ...]] = {
    # -- be / have / do -------------------------------------------------------------------
    "am": ("be",),
    "is": ("be",),
    "are": ("be",),
    "was": ("be",),
    "were": ("be",),
    "been": ("be",),
    "being": ("be",),
    "has": ("have",),
    "had": ("have",),
    "having": ("have",),
    "does": ("do",),
    "did": ("do",),
    "done": ("do",),
    "doing": ("do",),
    # -- irregular verbs (past / past participle) -----------------------------------------
    "arose": ("arise",),
    "arisen": ("arise",),
    "awoke": ("awake",),
    "awoken": ("awake",),
    "bore": ("bear",),
    "borne": ("bear",),
    "born": ("bear",),
    "beat": ("beat",),
    "beaten": ("beat",),
    "became": ("become",),
    "began": ("begin",),
    "begun": ("begin",),
    "bent": ("bend",),
    "bet": ("bet",),
    "bit": ("bite",),
    "bitten": ("bite",),
    "bled": ("bleed",),
    "blew": ("blow",),
    "blown": ("blow",),
    "broke": ("break",),
    "broken": ("break",),
    "bred": ("breed",),
    "brought": ("bring",),
    "built": ("build",),
    "burnt": ("burn",),
    "burst": ("burst",),
    "bought": ("buy",),
    "caught": ("catch",),
    "chose": ("choose",),
    "chosen": ("choose",),
    "clung": ("cling",),
    "came": ("come",),
    "cost": ("cost",),
    "crept": ("creep",),
    "cut": ("cut",),
    "dealt": ("deal",),
    "dug": ("dig",),
    "drew": ("draw",),
    "drawn": ("draw",),
    "dreamt": ("dream",),
    "drank": ("drink",),
    "drunk": ("drink",),
    "drove": ("drive",),
    "driven": ("drive",),
    "ate": ("eat",),
    "eaten": ("eat",),
    "fell": ("fall",),
    "fallen": ("fall",),
    "fed": ("feed",),
    "felt": ("feel",),
    "fought": ("fight",),
    "found": ("find",),
    "fled": ("flee",),
    "flew": ("fly",),
    "flown": ("fly",),
    "forbade": ("forbid",),
    "forbidden": ("forbid",),
    "forgot": ("forget",),
    "forgotten": ("forget",),
    "forgave": ("forgive",),
    "forgiven": ("forgive",),
    "froze": ("freeze",),
    "frozen": ("freeze",),
    "got": ("get",),
    "gotten": ("get",),
    "gave": ("give",),
    "given": ("give",),
    "went": ("go",),
    "gone": ("go",),
    "grew": ("grow",),
    "grown": ("grow",),
    "hung": ("hang",),
    "heard": ("hear",),
    "hid": ("hide",),
    "hidden": ("hide",),
    "hit": ("hit",),
    "held": ("hold",),
    "hurt": ("hurt",),
    "kept": ("keep",),
    "knelt": ("kneel",),
    "knew": ("know",),
    "known": ("know",),
    "laid": ("lay",),
    "led": ("lead",),
    "leant": ("lean",),
    "leapt": ("leap",),
    "learnt": ("learn",),
    "left": ("leave",),
    "lent": ("lend",),
    "let": ("let",),
    "lay": ("lie",),
    "lain": ("lie",),
    "lit": ("light",),
    "lost": ("lose",),
    "made": ("make",),
    "meant": ("mean",),
    "met": ("meet",),
    "paid": ("pay",),
    "put": ("put",),
    "quit": ("quit",),
    "read": ("read",),
    "rode": ("ride",),
    "ridden": ("ride",),
    "rang": ("ring",),
    "rung": ("ring",),
    "rose": ("rise",),
    "risen": ("rise",),
    "ran": ("run",),
    "said": ("say",),
    "saw": ("see",),
    "seen": ("see",),
    "sought": ("seek",),
    "sold": ("sell",),
    "sent": ("send",),
    "set": ("set",),
    "sewn": ("sew",),
    "shook": ("shake",),
    "shaken": ("shake",),
    "shone": ("shine",),
    "shot": ("shoot",),
    "showed": ("show",),
    "shown": ("show",),
    "shrank": ("shrink",),
    "shrunk": ("shrink",),
    "shut": ("shut",),
    "sang": ("sing",),
    "sung": ("sing",),
    "sank": ("sink",),
    "sunk": ("sink",),
    "sat": ("sit",),
    "slept": ("sleep",),
    "slid": ("slide",),
    "spoke": ("speak",),
    "spoken": ("speak",),
    "spent": ("spend",),
    "spilt": ("spill",),
    "spun": ("spin",),
    "spat": ("spit",),
    "split": ("split",),
    "spread": ("spread",),
    "sprang": ("spring",),
    "sprung": ("spring",),
    "stood": ("stand",),
    "stole": ("steal",),
    "stolen": ("steal",),
    "stuck": ("stick",),
    "stung": ("sting",),
    "stank": ("stink",),
    "stunk": ("stink",),
    "struck": ("strike",),
    "swore": ("swear",),
    "sworn": ("swear",),
    "swept": ("sweep",),
    "swam": ("swim",),
    "swum": ("swim",),
    "swung": ("swing",),
    "took": ("take",),
    "taken": ("take",),
    "taught": ("teach",),
    "tore": ("tear",),
    "torn": ("tear",),
    "told": ("tell",),
    "thought": ("think",),
    "threw": ("throw",),
    "thrown": ("throw",),
    "understood": ("understand",),
    "woke": ("wake",),
    "woken": ("wake",),
    "wore": ("wear",),
    "worn": ("wear",),
    "wept": ("weep",),
    "won": ("win",),
    "wound": ("wind",),
    "withdrew": ("withdraw",),
    "withdrawn": ("withdraw",),
    "wrote": ("write",),
    "written": ("write",),
    # -- irregular plurals ----------------------------------------------------------------
    "children": ("child",),
    "men": ("man",),
    "women": ("woman",),
    "feet": ("foot",),
    "teeth": ("tooth",),
    "geese": ("goose",),
    "mice": ("mouse",),
    "lice": ("louse",),
    "oxen": ("ox",),
    "people": ("person",),
    "lives": ("life",),
    "knives": ("knife",),
    "wives": ("wife",),
    "leaves": ("leaf",),
    "halves": ("half",),
    "wolves": ("wolf",),
    "shelves": ("shelf",),
    "thieves": ("thief",),
    "loaves": ("loaf",),
    "calves": ("calf",),
    "selves": ("self",),
    "criteria": ("criterion",),
    "phenomena": ("phenomenon",),
    "analyses": ("analysis",),
    "crises": ("crisis",),
    "theses": ("thesis",),
    "hypotheses": ("hypothesis",),
    "diagnoses": ("diagnosis",),
    "bases": ("basis",),
    "indices": ("index",),
    "matrices": ("matrix",),
    "appendices": ("appendix",),
    "vertices": ("vertex",),
    "cacti": ("cactus",),
    "fungi": ("fungus",),
    "nuclei": ("nucleus",),
    "radii": ("radius",),
    "alumni": ("alumnus",),
    "stimuli": ("stimulus",),
    "media": ("medium",),
    "bacteria": ("bacterium",),
    "curricula": ("curriculum",),
    # -- irregular comparison -------------------------------------------------------------
    "better": ("good",),
    "best": ("good",),
    "worse": ("bad",),
    "worst": ("bad",),
    "further": ("far",),
    "furthest": ("far",),
    "farther": ("far",),
    "farthest": ("far",),
    "least": ("little",),
    "elder": ("old",),
    "eldest": ("old",),
}
#: Read-only view, so the shared table cannot be mutated through a Deinflector.
_IRREGULAR: Mapping[str, tuple[str, ...]] = MappingProxyType(_IRREGULAR_FORMS)

# A stem shorter than this is noise ("as" -> "a"). Two is enough for real bases like "go".
_MIN_STEM = 2
# Only de-inflect words long enough to actually carry a suffix; "as"/"is" must be left alone.
_MIN_INFLECTED = 4
_MAX_VARIANTS = 6


@dataclass(frozen=True)
class Deinflector:
    """Strips English inflection suffixes to guess the base form(s) of a word.

    Owns the suffix rule table and the length thresholds that keep it from mangling short
    words, so a caller that needs different limits (a narrower cap for a cloze regex, say)
    builds its own instance instead of the module reaching for globals.

    Attributes:
        rules: ``(suffix, replacements)`` pairs, most specific first; the first matching
            suffix wins.
        min_stem: Shortest stem worth offering as a candidate.
        min_inflected: Shortest word worth de-inflecting at all.
        max_variants: Cap on the returned candidates, so a query stays small.
    """

    rules: tuple[tuple[str, tuple[str, ...]], ...] = _DEINFLECT
    irregular: Mapping[str, tuple[str, ...]] = _IRREGULAR
    min_stem: int = _MIN_STEM
    min_inflected: int = _MIN_INFLECTED
    max_variants: int = _MAX_VARIANTS

    def variants(self, word: str) -> tuple[str, ...]:
        """Return ``word`` plus plausible base forms of it, most-likely first.

        Double-clicking "loved" should still find the card filed under "love". A full
        lemmatiser would need a dictionary and a compiled dependency, neither of which can be
        vendored into an Anki add-on, so this is a small rule-based de-inflector: strip a known
        suffix, and also undo a doubled final consonant (``stopped`` -> ``stop``, ``running``
        -> ``run``).

        It is deliberately generous rather than clever — every candidate is OR-ed into one
        search, so a spurious form usually matches nothing, while a missing one loses the user
        their card.

        Args:
            word: The captured word.

        Returns:
            Deduped candidates including ``word`` itself (lowercased and stripped), capped at
            :attr:`max_variants`.
        """
        base = word.strip().lower()
        if not base:
            return ()
        found = [base]

        def offer(candidate: str) -> None:
            if len(candidate) >= self.min_stem and candidate not in found:
                found.append(candidate)

        # Irregulars first: they are exact knowledge, and they must be consulted BEFORE the
        # length guard because the shortest words in English are the most irregular ("ran",
        # "ate", "men", "was"). The suffix rules still run afterwards — a form can be both
        # ("leaves" is the plural of "leaf" AND the third person of "leave").
        for irregular_base in self.irregular.get(base, ()):
            offer(irregular_base)

        if len(base) < self.min_inflected:
            return tuple(found[: self.max_variants])
        for suffix, replacements in self.rules:
            if not base.endswith(suffix) or len(base) - len(suffix) < self.min_stem:
                continue
            stem = base[: -len(suffix)]
            doubled = len(stem) > 2 and stem[-1] == stem[-2] and stem[-1].isalpha()
            for replacement in replacements:
                # "stoppe"/"runne" are impossible words: a doubled consonant means the base LOST
                # an "e" (or never had one), so re-adding it only pads the query with noise.
                if replacement == "e" and doubled:
                    continue
                offer(stem + replacement)
            if doubled:
                offer(stem[:-1])  # stopped -> stop, running -> run
            break  # the first matching rule is the most specific one

        return tuple(found[: self.max_variants])


#: The shared de-inflector every caller gets unless it needs different thresholds.
DEFAULT_DEINFLECTOR = Deinflector()


def word_variants(word: str) -> tuple[str, ...]:
    """Return the word plus plausible base forms of it, most-likely first.

    Thin wrapper over :meth:`Deinflector.variants` on :data:`DEFAULT_DEINFLECTOR`, which is
    where the rules and the reasoning live.

    Args:
        word: The captured word.

    Returns:
        Deduped candidates including ``word`` itself, capped so the query stays small.
    """
    return DEFAULT_DEINFLECTOR.variants(word)


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
