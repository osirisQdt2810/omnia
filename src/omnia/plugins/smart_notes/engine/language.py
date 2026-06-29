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

from omnia.core import envs

if TYPE_CHECKING:
    from omnia.core.providers import ProviderHub
    from omnia.core.providers.llm.base import LLMProvider

_DETECT_SYSTEM = (
    "You are a language detector. Reply with ONLY the ISO 639-1 two-letter code of the "
    "language of the text the user sends (e.g. en, vi, ja, fr, es). No punctuation, no "
    "explanation, no quotes."
)
# The first bare two-letter token in the reply (tolerant of stray prose/markup).
_CODE_RE = re.compile(r"[a-z]{2}")


def detect_language(llm: LLMProvider, text: str, *, fallback: str = "en") -> str:
    """Return the ISO 639-1 code of ``text``'s language via ``llm`` (``fallback`` if unknown).

    Args:
        llm: A provider exposing ``generate_text``.
        text: The text whose language to detect (only a short prefix is sent).
        fallback: The code to return for blank text or an unparseable reply.

    Returns:
        A lowercase two-letter code, or ``fallback``.

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
    match = _CODE_RE.search((raw or "").strip().lower())
    return match.group(0) if match else fallback


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
