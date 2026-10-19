"""What the model is asked, and in what shape the answer must come back.

The prompt is the feature. Everything else in this plugin renders or remembers; this decides what
there is to render — and the difference between a useful correction and a useless one is almost
entirely here rather than in the model.

Four things it insists on, each because the obvious prompt gets them wrong:

* **Separate fixes, not one rewritten blob.** Asked plainly for "the corrected sentence", a model
  returns one, and the user learns nothing. Asked for a list, it explains each change on its own,
  which is what makes the panel worth reading twice.
* **The register decides what counts as wrong.** "I ain't got none" is a mistake in writing and
  ordinary in speech; "One must consider" is correct and absurd in conversation. A corrector with
  one standard is wrong half the time with total confidence.
* **Naturalness is a category, not a footnote.** Grammatically perfect sentences no native
  speaker would say are the ones a learner most needs flagged, and a model asked only about
  "errors" will pass them.
* **Leave correct text alone.** Models rewrite for style when nobody asked, and a panel that
  always finds five things to change teaches the reader to ignore it.
* **Ordered by what matters.** The panel shows the first few and the saved card keeps them all,
  so the ORDER decides what a reader in a hurry sees. A model left to its own devices returns
  them in the order they appear in the sentence, which puts a stray comma above a wrong tense
  whenever the comma came first.
* **A phrase in another language is translated, not refused.** The phrase may be written in any
  language. One that is not in the target language comes back as a natural translation, marked
  ``translated``, with one ``translation`` fix — told only "keep it in English", a model handed a
  Vietnamese paragraph answered with an empty rewrite, which reached the user as an error.

Pure string building: no provider, no HTTP, no ``aqt``. What comes back is parsed by
:mod:`omnia.plugins.phrase_check.correction`.
"""

from __future__ import annotations

import json
from typing import Any

from omnia.plugins.phrase_check.correction import (
    GRAMMAR,
    NATURALNESS,
    PUNCTUATION,
    SPELLING,
    SPOKEN,
    TRANSLATION,
    WORD_CHOICE,
    WRITTEN,
)

#: What each register means, in the model's own terms. Spelled out rather than named, because
#: "informal" alone gets slang and "formal" alone gets legalese. ``{target}`` is the language
#: the phrase is corrected into.
_REGISTER = {
    SPOKEN: (
        "SPOKEN {target} — how a fluent person would actually say this out loud in conversation. "
        "Contractions, short sentences and everyday word choice are correct here, not errors. "
        "Do not make it more formal than a native speaker would be."
    ),
    WRITTEN: (
        "WRITTEN {target} — how a fluent person would write this in an essay, an email or a "
        "report. Prefer full forms over contractions and complete sentences over fragments, but "
        "do not make it ornate or academic unless the original already is."
    ),
}

_KINDS = (GRAMMAR, WORD_CHOICE, NATURALNESS, SPELLING, PUNCTUATION)

#: The shape the answer must arrive in. Shown as an example rather than described, because a
#: model handed a filled-in example matches it far more reliably than one handed a schema.
_SHAPE = {
    "already_good": False,
    "translated": False,
    "rewritten": "the whole phrase with every fix applied",
    "fixes": [
        {
            "before": "the exact words from the original that are wrong",
            "after": 'what they should be, or "" to delete them',
            "kind": "one of: " + ", ".join(_KINDS),
            "why": "one or two sentences saying why, naming the rule where there is one",
        }
    ],
}


def build(
    text: str,
    *,
    mode: str = WRITTEN,
    language: str = "English",
    target_language: str = "English",
) -> str:
    """The prompt for one correction.

    Args:
        text: Exactly what the user selected, in any language.
        mode: :data:`~omnia.plugins.phrase_check.correction.SPOKEN` or ``WRITTEN``.
        language: What to write the EXPLANATIONS in — the language the reader thinks in.
        target_language: What the phrase is corrected INTO. A phrase written in another
            language is translated into it.

    Returns:
        The prompt.
    """
    target = target_language.strip() or "English"
    register = _REGISTER.get(mode, _REGISTER[WRITTEN]).format(target=target)
    return "\n".join(
        [
            "You are checking one phrase a language learner wrote. The phrase may be written "
            f"in any language; the corrected phrase is always in {target}.",
            "",
            f"Judge it as {register}",
            "",
            "Find every one of these that is genuinely present:",
            f"  - {GRAMMAR}: tense, agreement, articles, prepositions, word order",
            f"  - {WORD_CHOICE}: a real word used where a different one is meant",
            f"  - {SPELLING} and {PUNCTUATION}",
            f"  - {NATURALNESS}: correct {target} that no fluent speaker would actually say. "
            "This one matters. Say what they would say instead.",
            "",
            "Rules:",
            "  - One entry per change. Never bundle two unrelated problems into one entry.",
            "  - Order 'fixes' by how much each one matters to a reader: the mistake that most "
            "changes the meaning or most marks the writer as non-fluent first, a stray comma "
            "last. Only the first few may be shown, so the order is the judgement.",
            "  - 'rewritten' must still fix EVERYTHING you found, including anything far down "
            "the list. The order decides what is shown, never what is corrected.",
            "  - 'before' must be text copied EXACTLY from the phrase, so it can be found in it.",
            "  - Change nothing that is already correct. If the phrase is fine, say so with "
            "already_good: true, echo it back unchanged in 'rewritten', and return no fixes. "
            "Inventing changes to look useful is worse than finding nothing.",
            "  - Do not add information the writer did not put there.",
            f"  - Write every 'why' in {language}. 'rewritten' is always written in {target}.",
            f"  - If the phrase is not written in {target}, or is mostly in another language, "
            f"translate it: put a natural, fluent {target} version in 'rewritten', keeping "
            "proper nouns, technical terms and parenthesised glosses as a fluent writer would. "
            f'Then set "translated": true and return exactly ONE fix with kind '
            f"'{TRANSLATION}': 'before' is the whole phrase, 'after' is the translation, and "
            f"'why' says which language it was translated from (written in {language}). Never "
            "leave 'rewritten' empty.",
            "",
            "Answer with JSON in exactly this shape and nothing else:",
            json.dumps(_SHAPE, indent=2, ensure_ascii=False),
            "",
            "The phrase:",
            text.strip(),
        ]
    )


def language_name(code: Any) -> str:
    """A language code or name as something to put in a prompt.

    Falls back to English rather than to the raw value: a prompt that says "write every why in
    xx-XX" produces explanations in whatever the model guesses that means, and English is the one
    answer that is always readable by someone who chose to study in it.
    """
    text = str(code or "").strip()
    if not text:
        return "English"
    return _LANGUAGES.get(text.lower().replace("_", "-").split("-")[0], text)


#: Only the ones this add-on's own UI offers. A map that tried to cover every code would be a
#: table nobody maintains; an unrecognised value is passed through, which is right for a model
#: that understands "Bahasa Indonesia" perfectly well.
_LANGUAGES = {
    "en": "English",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}
