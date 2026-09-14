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
    WORD_CHOICE,
    WRITTEN,
)

#: What each register means, in the model's own terms. Spelled out rather than named, because
#: "informal" alone gets slang and "formal" alone gets legalese.
_REGISTER = {
    SPOKEN: (
        "SPOKEN English — how a fluent person would actually say this out loud in conversation. "
        "Contractions, short sentences and everyday word choice are correct here, not errors. "
        "Do not make it more formal than a native speaker would be."
    ),
    WRITTEN: (
        "WRITTEN English — how a fluent person would write this in an essay, an email or a "
        "report. Prefer full forms over contractions and complete sentences over fragments, but "
        "do not make it ornate or academic unless the original already is."
    ),
}

_KINDS = (GRAMMAR, WORD_CHOICE, NATURALNESS, SPELLING, PUNCTUATION)

#: The shape the answer must arrive in. Shown as an example rather than described, because a
#: model handed a filled-in example matches it far more reliably than one handed a schema.
_SHAPE = {
    "already_good": False,
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


def build(text: str, *, mode: str = WRITTEN, language: str = "English") -> str:
    """The prompt for one correction.

    Args:
        text: Exactly what the user selected.
        mode: :data:`~omnia.plugins.phrase_check.correction.SPOKEN` or ``WRITTEN``.
        language: What to write the EXPLANATIONS in. Not what to correct — the phrase is
            corrected as English either way; this is the language the reader thinks in.

    Returns:
        The prompt.
    """
    register = _REGISTER.get(mode, _REGISTER[WRITTEN])
    return "\n".join(
        [
            "You are checking one phrase a language learner wrote.",
            "",
            f"Judge it as {register}",
            "",
            "Find every one of these that is genuinely present:",
            f"  - {GRAMMAR}: tense, agreement, articles, prepositions, word order",
            f"  - {WORD_CHOICE}: a real word used where a different one is meant",
            f"  - {SPELLING} and {PUNCTUATION}",
            f"  - {NATURALNESS}: correct English that no fluent speaker would actually say. "
            "This one matters. Say what they would say instead.",
            "",
            "Rules:",
            "  - One entry per change. Never bundle two unrelated problems into one entry.",
            "  - 'before' must be text copied EXACTLY from the phrase, so it can be found in it.",
            "  - Change nothing that is already correct. If the phrase is fine, say so with "
            "already_good: true, echo it back unchanged in 'rewritten', and return no fixes. "
            "Inventing changes to look useful is worse than finding nothing.",
            "  - Do not add information the writer did not put there.",
            f"  - Write every 'why' in {language}. Keep the phrase itself in English.",
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
