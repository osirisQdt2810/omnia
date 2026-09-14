"""What a correction IS, before anything renders it or asks a model for one.

The user selects a phrase and asks what is wrong with it. The answer is not one blob of rewritten
text — it is a list of small, separate fixes, each with its own reason, and then the whole phrase
rewritten with all of them applied. That shape is the feature: "your sentence should be X" teaches
nothing, and one paragraph of prose explaining six unrelated problems is read by nobody.

So this module holds the shape and nothing else:

* a :class:`Fix` is ONE change — a word or a span — with what it was, what it becomes, and why;
* a :class:`Correction` is the fixes plus the rewritten phrase;
* :func:`highlight` marks which parts of the rewrite are new, so the page can bold exactly the
  words that changed and no others.

Two decisions worth stating, because both are easy to get wrong in the direction that looks fine:

* **A fix that changes nothing is dropped.** Models return them — same text, confident
  explanation — and a card saying "nothing → nothing" is a card the reader has to work out is
  noise. They are removed here rather than filtered in the page, so every surface agrees.
* **The rewrite is trusted over the fixes.** Applying the fixes by hand to rebuild the sentence
  sounds tidier and breaks the moment two of them overlap, or one is reworded by the model as it
  writes the final line. The model produces the sentence; the fixes explain it.

Pure: no HTTP, no ``aqt``, no provider. Given a dict it produces a value; that is the whole API.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: How formal the rewrite should be. The distinction the user asked for: the same sentence is
#: wrong in different ways depending on whether it is being said or written.
SPOKEN = "spoken"
WRITTEN = "written"
MODES = (SPOKEN, WRITTEN)

#: What a fix is about. Kept as an open string rather than an enum: a model will invent a
#: category eventually, and a correction that arrives with an unfamiliar label is still a useful
#: correction. The page groups by it and falls back to showing it verbatim.
GRAMMAR = "grammar"
WORD_CHOICE = "word choice"
NATURALNESS = "naturalness"
SPELLING = "spelling"
PUNCTUATION = "punctuation"


class CorrectionError(ValueError):
    """An answer that cannot be read as a correction, with a reason a person can act on."""


@dataclass(frozen=True)
class Fix:
    """One change, and why.

    Attributes:
        before: The exact text being replaced, as it appeared in the original.
        after: What it becomes. Empty means "delete this".
        why: The explanation, shown only when the reader asks for it — every fix carries one,
            and printing them all inline turns six small corrections into an essay.
        kind: Which sort of problem it is.
    """

    before: str
    after: str
    why: str = ""
    kind: str = GRAMMAR

    @property
    def is_deletion(self) -> bool:
        """Whether this fix removes text rather than replacing it."""
        return not self.after.strip()

    @property
    def changes_anything(self) -> bool:
        """Whether it is a real change.

        Whitespace-insensitive, because a model that returns ``"the  cat"`` → ``"the cat"`` with
        a paragraph about subject-verb agreement is not offering a correction, it is offering
        noise with a confident label on it. Case-SENSITIVE, because ``"i"`` → ``"I"`` is a real
        correction and one of the commonest there is (see :func:`_respace`).
        """
        return _respace(self.before) != _respace(self.after)


@dataclass(frozen=True)
class Correction:
    """Everything one request produced."""

    original: str
    rewritten: str
    mode: str = WRITTEN
    fixes: tuple[Fix, ...] = ()
    #: Set when the model found nothing worth changing. Not the same as an empty fix list after
    #: filtering: one means "this is fine", the other means "it answered with nothing usable".
    already_good: bool = False

    @property
    def changed(self) -> bool:
        """Whether the rewrite differs from what the user wrote."""
        return _respace(self.original) != _respace(self.rewritten)

    def highlighted(self) -> tuple[tuple[str, bool], ...]:
        """The rewrite as ``(text, is_new)`` runs, for bolding what changed."""
        return highlight(self.original, self.rewritten)


def parse(payload: Any, *, original: str, mode: str = WRITTEN) -> Correction:
    """Read a model's answer into a :class:`Correction`.

    Args:
        payload: The decoded JSON the model returned.
        original: What the user actually selected. Kept from the request rather than read back
            from the answer — a model that paraphrases the input in its echo would otherwise
            make the highlighting diff against a sentence nobody wrote.
        mode: Which register was asked for.

    Returns:
        The correction.

    Raises:
        CorrectionError: When the answer is not one, or carries no rewrite to show.
    """
    if not isinstance(payload, dict):
        raise CorrectionError("the model did not answer with a correction")
    rewritten = str(payload.get("rewritten") or "").strip()
    fixes = tuple(_fix(entry) for entry in _entries(payload.get("fixes")))
    fixes = tuple(fix for fix in fixes if fix.changes_anything)
    if not rewritten:
        # No sentence to show. Distinct from "nothing to fix", which arrives WITH the original
        # echoed back, and is a legitimate, useful answer.
        raise CorrectionError("the model answered without a corrected version")
    return Correction(
        original=original,
        rewritten=rewritten,
        mode=mode if mode in MODES else WRITTEN,
        fixes=fixes,
        # NOT `or not fixes`: an empty list after filtering means "it answered with nothing
        # usable", which is the opposite of "this is fine". A model that names its keys
        # differently enough for every fix to be dropped would otherwise have the panel say the
        # sentence is correct while showing a REWRITTEN one beside it.
        # Derived, never taken on the model's word. A payload claiming ``already_good`` beside
        # a rewrite that differs is a reassurance contradicted by the sentence printed under it,
        # and the panel renders the two together — so the sentences decide.
        already_good=not fixes and _respace(original) == _respace(rewritten),
    )


def highlight(original: str, rewritten: str) -> tuple[tuple[str, bool], ...]:
    """Split ``rewritten`` into runs, marking which are new.

    Word-level rather than character-level: a character diff on "recieve" → "receive" bolds two
    letters in the middle of a word, which is unreadable at body-text size. A word is the
    smallest unit a reader can actually see has changed.

    Punctuation travels with its word, so a fix that only adds a comma still bolds something —
    marking a bare "," on its own line would be invisible.

    Returns:
        ``((text, is_new), …)``, in order, joinable back into the rewrite exactly.
    """
    before, after = _words(original), _words(rewritten)
    runs: list[tuple[str, bool]] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
        None,
        [_respace(w) for w in before],
        [_respace(w) for w in after],
        autojunk=False,
    ).get_opcodes():
        if tag == "delete":
            continue
        chunk = "".join(after[j1:j2])
        if chunk:
            runs.append((chunk, tag != "equal"))
    return _merge(runs)


def _merge(runs: Iterable[tuple[str, bool]]) -> tuple[tuple[str, bool], ...]:
    """Join neighbouring runs that carry the same mark, so the page renders fewer spans."""
    out: list[tuple[str, bool]] = []
    for text, is_new in runs:
        if out and out[-1][1] == is_new:
            out[-1] = (out[-1][0] + text, is_new)
        else:
            out.append((text, is_new))
    return tuple(out)


#: A word plus whatever trails it. Splitting on whitespace alone would lose the spaces, and the
#: runs have to join back into the exact rewrite or the page shows text nobody wrote.
_WORD_RE = re.compile(r"\S+\s*")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _respace(text: str) -> str:
    """For comparison only: collapse whitespace, and nothing else.

    Case is NOT collapsed, because case is one of the things being corrected. Folding it made
    every capitalisation fix invisible to this module: ``i went`` → ``I went`` compared equal, so
    the fix was filtered out as noise, ``already_good`` came out True, and the panel told the
    user the sentence was fine beside a sentence that differed from what they wrote. Missing
    capitals on "I" and at the start of a sentence are among the commonest written-English
    learner errors, and ``written`` is the register this ships in.

    The de-capitalising direction was the case this was written for — a model "fixing" a capital
    it invented — but a rule that discards those also discards the corrections that are right,
    and being silent about a real mistake is the worse half of that trade.

    Lines up with :meth:`CacheKey.digest`, which already normalises whitespace only, on the
    stated grounds that "i went" and "I went" are different questions.
    """
    return " ".join((text or "").split())


def _entries(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value or () if isinstance(entry, dict)]


def _fix(entry: dict[str, Any]) -> Fix:
    return Fix(
        before=str(entry.get("before") or ""),
        after=str(entry.get("after") or ""),
        why=str(entry.get("why") or entry.get("explanation") or "").strip(),
        kind=str(entry.get("kind") or entry.get("type") or GRAMMAR).strip().lower(),
    )
