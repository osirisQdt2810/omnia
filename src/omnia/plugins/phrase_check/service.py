"""Turning "check this phrase" into a correction, with the model asked at most once.

The three pure pieces meet here: :mod:`.prompt` decides what to ask, :mod:`.correction` decides
what an answer is, and :mod:`.cache` decides when not to ask at all. This module is the order they
go in, and nothing else — no HTTP of its own, no ``aqt``, and the provider arrives as an argument
so a test drives the whole path with a fake.

Two things it will not do, both of which look like helpfulness:

* **It does not retry a model that answered.** A reply that cannot be parsed is reported, not
  re-rolled. Sampling again on a bad answer turns one wrong correction into two charges and a
  coin flip, and the user has no idea either happened.
* **It does not cache a failure.** A provider that was rate-limited for ten seconds would
  otherwise be remembered as "this phrase cannot be corrected" for a month.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.plugins.phrase_check import prompt as prompt_module
from omnia.plugins.phrase_check.cache import CacheKey, CorrectionCache
from omnia.plugins.phrase_check.correction import (
    WRITTEN,
    Correction,
    CorrectionError,
    parse,
)

logger = get_logger("phrase_check")

#: The longest phrase accepted. Not a cost guard — it is that beyond a paragraph or two the
#: answer stops being a list of fixes a person reads and becomes an essay they skim, which is the
#: failure this whole feature is shaped to avoid.
MAX_CHARACTERS = 2000

#: Low, because this is not a creative task. The same sentence should get the same correction on
#: Tuesday as it did on Monday, and a model asked to be inventive about grammar invents grammar.
TEMPERATURE = 0.2


class PhraseCheckError(RuntimeError):
    """A correction that could not be produced, with a reason a person can act on.

    The attribute is the contract with ``word_lookup``, which serves this over HTTP and may not
    import it (ADR-019). It marks the message as one deliberately WRITTEN for a person: the
    provider's own sentence, or a limit being named. Anything without it is an internal detail —
    an ``AttributeError``, a repr with a request URL and a key in it — and is answered with a
    flat 500 instead of being echoed to a clipper.
    """

    #: See the class docstring. Read by name, never by type, so no import crosses plugins.
    user_facing = True


class PhraseChecker:
    """Corrects one phrase, asking the model only when the answer is not already known.

    Args:
        providers: The provider hub. ``llm()`` is called per request rather than once at
            construction, so changing the model in settings takes effect on the next correction
            instead of on the next restart.
        cache: Where answers are remembered. ``None`` disables it, which is what a test wants.
        language: What to write the explanations in.
        model: Pin a specific model, or "" for the configured one.
    """

    def __init__(
        self,
        providers: Any,
        cache: Optional[CorrectionCache] = None,
        *,
        language: str = "English",
        model: str = "",
    ) -> None:
        self._providers = providers
        self._cache = cache
        self._language = language
        self._model = model

    def check(
        self, text: str, *, mode: str = WRITTEN, refresh: bool = False
    ) -> Correction:
        """Correct ``text``.

        Args:
            text: Exactly what the user selected.
            mode: ``spoken`` or ``written`` — the register decides what counts as wrong.
            refresh: Ask again even if the answer is remembered. What a "check it again" button
                sends, and the only thing that spends money on a phrase already answered.

        Returns:
            The correction.

        Raises:
            PhraseCheckError: When there is nothing to check, when it is too long, or when the
                model could not be reached or could not be read.
        """
        phrase = (text or "").strip()
        if not phrase:
            raise PhraseCheckError("There is nothing selected to check.")
        if len(phrase) > MAX_CHARACTERS:
            raise PhraseCheckError(
                f"That is too long to check at once — {len(phrase):,} characters, and the "
                f"limit is {MAX_CHARACTERS:,}. Select a sentence or two."
            )

        key = CacheKey(
            text=phrase, mode=mode, language=self._language, model=self._model
        )
        if self._cache is not None and not refresh:
            remembered = self._cache.get(key)
            if remembered is not None:
                logger.debug("phrase_check: answered from the cache")
                # Re-read against the phrase the answer was FOR, not the one just selected.
                # They differ by punctuation at the edges — that is what the key ignores — and
                # `already_good`, `changed` and the marked words are all derived by comparing
                # the original with the rewrite. Reading a hit against the wrong sentence
                # reports a correct phrase as corrected, marks a full stop nobody wrote, and
                # lists no fix explaining it.
                return self._read(remembered.payload, remembered.text or phrase, mode)

        payload = self._ask(phrase, mode)
        correction = self._read(payload, phrase, mode)
        if self._cache is not None:
            # After parsing, never before: an answer that cannot be read is not one to remember
            # for a month.
            self._cache.put(key, payload)
        return correction

    # --- the model ------------------------------------------------------------------------
    def _ask(self, phrase: str, mode: str) -> dict[str, Any]:
        from omnia.core.providers import ProviderError

        try:
            provider = self._providers.llm(model=self._model)
        except Exception as exc:
            raise PhraseCheckError(_provider_failure(exc)) from exc
        try:
            answer = provider.generate_text(
                prompt_module.build(phrase, mode=mode, language=self._language),
                temperature=TEMPERATURE,
            )
        except ProviderError as exc:
            raise PhraseCheckError(_provider_failure(exc)) from exc
        except Exception as exc:
            logger.exception("phrase_check: the model call failed")
            raise PhraseCheckError(
                "Something went wrong asking the model — see the Omnia log."
            ) from exc
        return _decode(answer)

    def _read(self, payload: dict[str, Any], phrase: str, mode: str) -> Correction:
        try:
            return parse(payload, original=phrase, mode=mode)
        except CorrectionError as exc:
            # Reported, never re-rolled: sampling again on a bad answer turns one wrong
            # correction into two charges and a coin flip, with nothing on screen saying either
            # happened.
            raise PhraseCheckError(
                f"The model's answer could not be read — {exc}."
            ) from exc


def _decode(answer: str) -> dict[str, Any]:
    """The JSON object out of a model's reply.

    Tolerant of the two things every model does to JSON it was asked for: wrapping it in a
    ```json fence, and saying a sentence before it. Not tolerant of anything else — a reply with
    no object in it is a reply to report, not to guess at.

    Raises:
        PhraseCheckError: When there is no object to read.
    """
    text = (answer or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        # Not a bare json.loads on the whole reply: a model that prefixes "Here is the
        # correction:" produces valid JSON that json.loads still refuses.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PhraseCheckError(
                "The model did not answer with a correction."
            ) from None
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            raise PhraseCheckError(
                "The model did not answer with a correction."
            ) from None
    if not isinstance(payload, dict):
        raise PhraseCheckError("The model did not answer with a correction.")
    return payload


def _provider_failure(exc: BaseException) -> str:
    """What a provider failure means, in words that name the next action.

    The provider's own message is kept where there is one: it carries the status and the
    provider's own explanation, which is the difference between "check your key" and "you are out
    of credit" — and both are things only the user can fix.
    """
    detail = str(exc).strip()
    if not detail:
        return "Could not reach the language model. Check the Omnia provider settings."
    return f"Could not check that phrase — {detail}"
