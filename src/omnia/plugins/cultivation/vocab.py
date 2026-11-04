"""Words, modalities and vocabulary power for the Anki domain (spec §4.3, §4.4, §4.8).

The unit of knowledge is the WORD, not the card: notes whose normalised word matches are merged,
and a word's strength is the best evidence among its cards::

    κ_w(t) = max over reviewed cards c of w:  μ(modality(c)) · R_c(t)
    power(t) = Σ_b B_b + Σ_w κ_w(t)

μ ≤ 1, so power is a conservative "word-equivalents" vocabulary-size estimate — which is what lets
the realm thresholds be published vocabulary-size milestones.

Pure: no ``aqt``/``anki``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

from omnia.plugins.cultivation.config import (
    ACTIVE_RECALL,
    ACTIVE_RECALL_TYPED,
    PASSIVE_RECALL,
)
from omnia.plugins.cultivation.memory import CardMemory

_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_SPACE_RE = re.compile(r"\s+")
_FIELD_REF_RE = re.compile(r"{{([^{}]+)}}")

# Field-name hints for the Setup tab's suggestions only; the user confirms every pick.
_WORD_HINTS = ("word", "front", "term", "expression", "vocab", "headword", "english")


def clean_field(raw: str) -> str:
    """A field's visible text: no HTML, no ``[sound:]``, entities decoded, spaces collapsed."""
    text = _SOUND_RE.sub(" ", raw or "")
    text = _TAG_RE.sub(" ", text.replace("<br>", " "))
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()


def normalize_word(raw: str) -> str:
    """The merge key for a word: cleaned, lowercased, trimmed (spec §4.3)."""
    return clean_field(raw).lower()


def template_key(note_type: str, template: str) -> str:
    """``"Note type/Template"`` — the key ``CultivationSettings.modality`` uses."""
    return f"{note_type}/{template}"


def is_typed_template(front: str, back: str) -> bool:
    """A typing card: ``{{type:…}}`` anywhere in the template."""
    return "{{type:" in (front or "") or "{{type:" in (back or "")


def _references(template: str, field_name: str) -> bool:
    """Whether a template shows ``field_name`` (any filter prefix, e.g. ``{{text:Word}}``)."""
    for ref in _FIELD_REF_RE.findall(template or ""):
        name = ref.split(":")[-1].strip()
        if name == field_name and not ref.lstrip().startswith(("#", "^", "/")):
            return True
    return False


def suggest_modality(front: str, back: str, word_field: Optional[str]) -> str:
    """The Setup tab's auto-suggestion for a template's modality.

    ``{{type:}}`` → typed active recall; a front that shows the word → passive recall (word →
    meaning); otherwise the word is what you must produce → active recall. Recognition (choose
    among options) cannot be told from a template, so it is never suggested — only picked.
    """
    if is_typed_template(front, back):
        return ACTIVE_RECALL_TYPED
    if word_field and _references(front, word_field):
        return PASSIVE_RECALL
    return ACTIVE_RECALL


def _suggest(
    fields: Sequence[str], hints: Sequence[str], skip: Optional[str]
) -> Optional[str]:
    for hint in hints:
        for name in fields:
            if name != skip and hint in name.lower():
                return name
    return None


def suggest_word_field(fields: Sequence[str]) -> Optional[str]:
    """The field most likely to hold the English word (falls back to the first field)."""
    return _suggest(fields, _WORD_HINTS, None) or (fields[0] if fields else None)


@dataclass(frozen=True)
class VocabCard:
    """One card of a vocabulary note, already mapped to its word and modality."""

    cid: int
    word: str
    modality: str
    memory: CardMemory


@dataclass
class Word:
    """A word and all the cards that carry evidence about it."""

    word: str
    cards: list[VocabCard] = field(default_factory=list)

    def counted(self, since_s: float) -> bool:
        """Whether any card of the word was reviewed at or after ``since_s``."""
        return any((c.memory.last_review_s or -1) >= since_s for c in self.cards)


def group_words(cards: Iterable[VocabCard]) -> dict[str, Word]:
    """Merge cards into words by normalised word; empty words are dropped."""
    words: dict[str, Word] = {}
    for card in cards:
        if card.word:
            words.setdefault(card.word, Word(card.word)).cards.append(card)
    return words


class VocabularyPower:
    """Computes κ_w and power for a set of words.

    Args:
        weights: μ per modality.
        since_s: Only cards whose last review is at or after this moment count — the "counting
            since" start day. A card reviewed before it and not since adds nothing until it is
            reviewed again, which is what makes a fresh start begin at zero.
        baseline: Σ_b B_b — words known outside Anki (a vocabulary-size test). 0 until one is
            taken; the window says so.
    """

    def __init__(
        self, weights: Mapping[str, float], since_s: float = 0.0, baseline: float = 0.0
    ) -> None:
        self.weights = dict(weights)
        self.since_s = since_s
        self.baseline = baseline

    def strength(
        self,
        word: Word,
        now_s: float,
        states: Optional[Mapping[int, CardMemory]] = None,
    ) -> float:
        """κ_w at ``now_s``; ``states`` overrides card memories (for the §4.8 gain)."""
        best = 0.0
        for card in word.cards:
            memory = (
                states.get(card.cid, card.memory) if states is not None else card.memory
            )
            if (memory.last_review_s or -1) < self.since_s:
                continue
            mu = self.weights.get(card.modality, 0.0)
            best = max(best, mu * memory.r(now_s))
        return best

    def power(
        self,
        words: Mapping[str, Word],
        now_s: float,
        states: Optional[Mapping[int, CardMemory]] = None,
    ) -> float:
        """``Σ B_b + Σ_w κ_w`` at ``now_s``."""
        return self.baseline + sum(
            self.strength(w, now_s, states) for w in words.values()
        )


def start_of_day_states(
    cards: Iterable[VocabCard],
    day_start_s: float,
    snapshot: Mapping[int, CardMemory],
) -> dict[int, CardMemory]:
    """Each card's memory as it was BEFORE today's reviews (spec §4.8).

    A card not reviewed today is unchanged. A card reviewed today takes its stored snapshot (the
    state last seen before today); without one it counts as unreviewed — so a card first learnt
    today adds its whole κ to today's gain, which is exactly what learning it did.
    """
    states: dict[int, CardMemory] = {}
    for card in cards:
        last = card.memory.last_review_s
        if last is None or last < day_start_s:
            states[card.cid] = card.memory
        else:
            before = snapshot.get(card.cid)
            if before is not None and (before.last_review_s or 0) < day_start_s:
                states[card.cid] = before
            else:
                states[card.cid] = CardMemory(
                    card.memory.stability, card.memory.decay, None
                )
    return states
