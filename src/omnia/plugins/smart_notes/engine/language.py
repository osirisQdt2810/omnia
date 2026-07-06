"""Best-effort language detection for TTS (LLM-backed).

A sound field should be spoken in the language of its text — a Vietnamese word read by an
English voice is useless. This module asks the configured LLM for the language of a short
snippet and returns a lowercase ISO 639-1 code (e.g. ``"en"``, ``"vi"``, ``"ja"``). It is
best-effort: the caller treats any failure or unrecognised reply as "use the configured
default voice/language", so audio generation never fails just because detection did.

:func:`detect_language` is the raw LLM call; :class:`LanguageDetector` is the best-effort
wrapper injected into the TTS generator, swallowing every failure into "no language hint".

Pure module — it takes a duck-typed provider (anything with ``generate_text``) and imports
nothing from ``aqt``/``anki``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from omnia import envs

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.core.providers.llm.base import LLMProvider

_DETECT_SYSTEM = (
    "You are a language detector. Reply with ONLY the ISO 639-1 two-letter code of the "
    "language of the text the user sends (e.g. en, vi, ja, fr, es). No punctuation, no "
    "explanation, no quotes."
)
# Standalone two-letter tokens in the reply (word boundaries so a 2-letter run inside a longer
# word like "Spanish" is NOT treated as a code). Each candidate is validated against
# _ISO_639_1 below before being accepted, so a prose reply falls back rather than emitting junk.
_CODE_RE = re.compile(r"\b[a-z]{2}\b")

# The full ISO 639-1 two-letter language codes. A parsed candidate must be one of these to be
# accepted; anything else (a stray English word like "is"/"an" the model returned in prose) is
# rejected in favour of the fallback.
_ISO_639_1 = frozenset(
    {
        "aa",
        "ab",
        "ae",
        "af",
        "ak",
        "am",
        "an",
        "ar",
        "as",
        "av",
        "ay",
        "az",
        "ba",
        "be",
        "bg",
        "bh",
        "bi",
        "bm",
        "bn",
        "bo",
        "br",
        "bs",
        "ca",
        "ce",
        "ch",
        "co",
        "cr",
        "cs",
        "cu",
        "cv",
        "cy",
        "da",
        "de",
        "dv",
        "dz",
        "ee",
        "el",
        "en",
        "eo",
        "es",
        "et",
        "eu",
        "fa",
        "ff",
        "fi",
        "fj",
        "fo",
        "fr",
        "fy",
        "ga",
        "gd",
        "gl",
        "gn",
        "gu",
        "gv",
        "ha",
        "he",
        "hi",
        "ho",
        "hr",
        "ht",
        "hu",
        "hy",
        "hz",
        "ia",
        "id",
        "ie",
        "ig",
        "ii",
        "ik",
        "io",
        "is",
        "it",
        "iu",
        "ja",
        "jv",
        "ka",
        "kg",
        "ki",
        "kj",
        "kk",
        "kl",
        "km",
        "kn",
        "ko",
        "kr",
        "ks",
        "ku",
        "kv",
        "kw",
        "ky",
        "la",
        "lb",
        "lg",
        "li",
        "ln",
        "lo",
        "lt",
        "lu",
        "lv",
        "mg",
        "mh",
        "mi",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "mt",
        "my",
        "na",
        "nb",
        "nd",
        "ne",
        "ng",
        "nl",
        "nn",
        "no",
        "nr",
        "nv",
        "ny",
        "oc",
        "oj",
        "om",
        "or",
        "os",
        "pa",
        "pi",
        "pl",
        "ps",
        "pt",
        "qu",
        "rm",
        "rn",
        "ro",
        "ru",
        "rw",
        "sa",
        "sc",
        "sd",
        "se",
        "sg",
        "si",
        "sk",
        "sl",
        "sm",
        "sn",
        "so",
        "sq",
        "sr",
        "ss",
        "st",
        "su",
        "sv",
        "sw",
        "ta",
        "te",
        "tg",
        "th",
        "ti",
        "tk",
        "tl",
        "tn",
        "to",
        "tr",
        "ts",
        "tt",
        "tw",
        "ty",
        "ug",
        "uk",
        "ur",
        "uz",
        "ve",
        "vi",
        "vo",
        "wa",
        "wo",
        "xh",
        "yi",
        "yo",
        "za",
        "zh",
        "zu",
    }
)


def detect_language(llm: LLMProvider, text: str, *, fallback: str = "en") -> str:
    """Return the ISO 639-1 code of ``text``'s language via ``llm`` (``fallback`` if unknown).

    Args:
        llm: A provider exposing ``generate_text``.
        text: The text whose language to detect (only a short prefix is sent).
        fallback: The code to return for blank text or a reply with no recognised ISO 639-1 code.

    Returns:
        A lowercase two-letter ISO 639-1 code, or ``fallback``.

    Raises:
        ProviderError: Propagated from the provider — callers that want best-effort behaviour
            should guard the call.
    """
    snippet = (text or "").strip()
    if not snippet:
        return fallback
    raw = llm.generate_text(
        snippet[:400],
        system=_DETECT_SYSTEM,
        temperature=envs.OMNIA_SMART_NOTES_DETECT_LANGUAGE_TEMPERATURE,
        max_tokens=8,
    )
    for token in _CODE_RE.findall((raw or "").lower()):
        if token in _ISO_639_1:
            return token
    return fallback


class LanguageDetector:
    """Best-effort spoken-language detection injected into the TTS generator.

    Swallows every error (no LLM configured, provider/network failure, …): detection is a
    nicety, so a failure must fall back to the provider's configured language rather than
    break audio generation. A no-op (returns None) when disabled or the text is blank.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def detect(self, providers: ProviderHub, text: str) -> Optional[str]:
        """Return a best-effort language code for ``text`` (None when disabled/unavailable)."""
        if not self._enabled or not text.strip():
            return None
        try:
            return detect_language(providers.llm(), text, fallback="") or None
        except Exception:  # best-effort: any failure → provider's configured default
            return None
