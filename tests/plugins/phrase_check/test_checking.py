"""Tests for turning "check this phrase" into a correction.

The three pure pieces meet here, so what is under test is the ORDER they go in and the two things
this refuses to do — both of which look like helpfulness:

* it does not retry a model that answered, because sampling again on a bad reply turns one wrong
  correction into two charges and a coin flip, with nothing on screen saying either happened;
* it does not cache a failure, because a provider that was rate-limited for ten seconds would
  otherwise be remembered as "this phrase cannot be corrected" for a month.
"""

from __future__ import annotations

import json

import pytest

from omnia.plugins.phrase_check.cache import CorrectionCache
from omnia.plugins.phrase_check.correction import SPOKEN, WRITTEN
from omnia.plugins.phrase_check.service import (
    MAX_CHARACTERS,
    PhraseChecker,
    PhraseCheckError,
)

ANSWER = {
    "rewritten": "I have gone to the shop.",
    "fixes": [
        {
            "before": "have went",
            "after": "have gone",
            "why": "The past participle of 'go' is 'gone'.",
            "kind": "grammar",
        }
    ],
}
PHRASE = "I have went to the shop."


class _Provider:
    def __init__(self, reply=None, *, fail: Exception | None = None) -> None:
        self.reply = json.dumps(ANSWER) if reply is None else reply
        self.fail = fail
        self.prompts: list[str] = []
        self.temperatures: list[float] = []

    def generate_text(self, prompt, *, temperature=None, **_kwargs):
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        if self.fail is not None:
            raise self.fail
        return self.reply


class _Hub:
    def __init__(self, provider=None, *, fail: Exception | None = None) -> None:
        self.provider = provider or _Provider()
        self.fail = fail
        self.built = 0

    def llm(self, *, model="", **_kwargs):
        self.built += 1
        if self.fail is not None:
            raise self.fail
        return self.provider


def _cache() -> CorrectionCache:
    store: dict = {}

    def write(new: dict) -> None:
        store.clear()
        store.update(new)

    return CorrectionCache(lambda: store, write)


class TestAStraightCheck:
    def test_it_returns_the_fixes_and_the_rewrite(self):
        checker = PhraseChecker(_Hub())

        correction = checker.check(PHRASE)

        assert correction.rewritten == "I have gone to the shop."
        assert [fix.before for fix in correction.fixes] == ["have went"]

    def test_the_original_is_what_the_caller_sent(self):
        # Not read back out of the answer: a model that paraphrases the input in its echo would
        # make the highlighting diff against a sentence nobody wrote.
        correction = PhraseChecker(_Hub()).check("  " + PHRASE + "  ")

        assert correction.original == PHRASE

    def test_the_register_reaches_the_prompt(self):
        hub = _Hub()

        PhraseChecker(hub).check(PHRASE, mode=SPOKEN)

        assert "SPOKEN English" in hub.provider.prompts[0]

    def test_the_explanation_language_reaches_the_prompt(self):
        hub = _Hub()

        PhraseChecker(hub, language="Vietnamese").check(PHRASE)

        assert "Write every 'why' in Vietnamese" in hub.provider.prompts[0]

    def test_it_asks_for_a_low_temperature(self):
        # The same sentence should get the same correction on Tuesday as on Monday, and a model
        # asked to be inventive about grammar invents grammar.
        hub = _Hub()

        PhraseChecker(hub).check(PHRASE)

        assert hub.provider.temperatures[0] <= 0.3

    def test_the_provider_is_built_per_request(self):
        # So changing the model in settings takes effect on the next correction rather than on
        # the next restart.
        hub = _Hub()
        checker = PhraseChecker(hub)

        checker.check(PHRASE)
        checker.check("Another phrase entirely.")

        assert hub.built == 2


class TestAskingAtMostOnce:
    def test_the_second_identical_check_does_not_reach_the_model(self):
        hub = _Hub()
        checker = PhraseChecker(hub, _cache())

        checker.check(PHRASE)
        checker.check(PHRASE)

        assert len(hub.provider.prompts) == 1

    def test_a_different_register_is_a_different_question(self):
        hub = _Hub()
        checker = PhraseChecker(hub, _cache())

        checker.check(PHRASE, mode=WRITTEN)
        checker.check(PHRASE, mode=SPOKEN)

        assert len(hub.provider.prompts) == 2

    def test_refreshing_asks_again(self):
        # What a "check it again" button sends, and the only thing that spends money on a phrase
        # already answered.
        hub = _Hub()
        checker = PhraseChecker(hub, _cache())
        checker.check(PHRASE)

        checker.check(PHRASE, refresh=True)

        assert len(hub.provider.prompts) == 2

    def test_a_cached_answer_still_comes_back_as_a_correction(self):
        checker = PhraseChecker(_Hub(), _cache())
        checker.check(PHRASE)

        again = checker.check(PHRASE)

        assert again.rewritten == "I have gone to the shop."
        assert len(again.fixes) == 1


class TestACacheHitReadsAsItsOwnAnswer:
    """The half a digest test cannot see: what a HIT actually renders as.

    The key ignores punctuation at the edges, so a stored answer is served for a phrase spelled
    slightly differently from the one that produced it. Everything a panel shows —
    ``already_good``, ``changed``, which words are marked — is derived by comparing the original
    with the rewrite, so a hit read against the wrong sentence looks like a correction nobody
    asked for.
    """

    def test_a_correct_sentence_does_not_become_a_corrected_one(self):
        # The exact failure: ask with the full stop, get "nothing to change"; ask again without
        # it and the panel said the sentence had been changed, bolded a stop nobody wrote, and
        # listed no fix explaining it.
        echoed = json.dumps({"rewritten": "It is fine.", "fixes": []})
        checker = PhraseChecker(_Hub(_Provider(echoed)), _cache())

        first = checker.check("It is fine.")
        hit = checker.check("It is fine")

        assert first.already_good is True
        assert (
            hit.already_good is True
        ), "a cache hit reported a correct sentence as corrected"
        assert hit.changed is False
        assert hit.fixes == ()
        assert not any(is_new for _text, is_new in hit.highlighted())

    def test_it_is_answered_from_the_cache_at_all(self):
        # Guards the test above: if the second call reached the model, everything it asserts
        # would be true for the wrong reason.
        hub = _Hub(_Provider(json.dumps({"rewritten": "It is fine.", "fixes": []})))
        checker = PhraseChecker(hub, _cache())

        checker.check("It is fine.")
        checker.check("It is fine")

        assert len(hub.provider.prompts) == 1

    def test_a_fix_is_not_replayed_against_text_that_does_not_have_it(self):
        # The same mechanism the other way: an answer cached for a phrase WITHOUT the stop,
        # served for one with it, must not advise adding a stop that is already there.
        checker = PhraseChecker(_Hub(), _cache())

        first = checker.check(PHRASE)
        hit = checker.check(PHRASE.rstrip("."))

        assert hit.rewritten == first.rewritten
        assert [f.before for f in hit.fixes] == [f.before for f in first.fixes]
        assert hit.already_good == first.already_good
        assert hit.changed == first.changed

    def test_the_marked_words_are_the_same_either_way(self):
        checker = PhraseChecker(_Hub(), _cache())

        first = checker.check(PHRASE)
        hit = checker.check(PHRASE + " ")

        assert hit.highlighted() == first.highlighted()


class TestWhatItRefusesToDo:
    def test_an_unreadable_answer_is_not_retried(self):
        hub = _Hub(_Provider("this is not JSON at all"))

        with pytest.raises(PhraseCheckError):
            PhraseChecker(hub).check(PHRASE)

        assert len(hub.provider.prompts) == 1, "it sampled again on a bad answer"

    def test_a_failure_is_not_remembered(self):
        # A provider rate-limited for ten seconds must not be cached as "this phrase cannot be
        # corrected" for a month.
        hub = _Hub(_Provider("not JSON"))
        cache = _cache()
        checker = PhraseChecker(hub, cache)

        with pytest.raises(PhraseCheckError):
            checker.check(PHRASE)

        assert len(cache) == 0

    def test_an_answer_with_no_rewrite_is_not_remembered_either(self):
        hub = _Hub(_Provider(json.dumps({"fixes": []})))
        cache = _cache()

        with pytest.raises(PhraseCheckError, match="could not be read"):
            PhraseChecker(hub, cache).check(PHRASE)

        assert len(cache) == 0


class TestWhatItRefusesToAccept:
    def test_nothing_selected_says_so(self):
        for text in ("", "   ", "\n\t "):
            with pytest.raises(PhraseCheckError, match="nothing selected"):
                PhraseChecker(_Hub()).check(text)

    def test_nothing_selected_never_reaches_the_model(self):
        hub = _Hub()

        with pytest.raises(PhraseCheckError):
            PhraseChecker(hub).check("")

        assert hub.built == 0

    def test_something_far_too_long_says_how_long_it_is(self):
        # Beyond a paragraph the answer stops being a list of fixes somebody reads and becomes
        # an essay they skim, which is the failure this feature is shaped to avoid.
        with pytest.raises(PhraseCheckError, match="too long"):
            PhraseChecker(_Hub()).check("word " * (MAX_CHARACTERS // 2))


class TestReadingAModelThatDoesNotFollowInstructions:
    def test_a_fenced_answer_is_read(self):
        reply = "```json\n" + json.dumps(ANSWER) + "\n```"

        assert PhraseChecker(_Hub(_Provider(reply))).check(PHRASE).fixes

    def test_a_bare_fence_without_the_language_is_read_too(self):
        reply = "```\n" + json.dumps(ANSWER) + "\n```"

        assert PhraseChecker(_Hub(_Provider(reply))).check(PHRASE).fixes

    def test_a_sentence_before_the_json_is_tolerated(self):
        # json.loads refuses this even though the JSON in it is perfectly valid.
        reply = "Here is the correction:\n" + json.dumps(ANSWER)

        assert PhraseChecker(_Hub(_Provider(reply))).check(PHRASE).fixes

    def test_a_reply_with_no_object_at_all_is_reported_not_guessed_at(self):
        with pytest.raises(PhraseCheckError, match="did not answer with a correction"):
            PhraseChecker(_Hub(_Provider("I'm sorry, I can't help with that."))).check(
                PHRASE
            )

    def test_a_json_list_is_not_a_correction(self):
        with pytest.raises(PhraseCheckError, match="did not answer with a correction"):
            PhraseChecker(_Hub(_Provider("[1, 2, 3]"))).check(PHRASE)


class TestWhenTheProviderFails:
    def test_the_providers_own_reason_survives(self):
        # It carries the status and the provider's explanation, which is the difference between
        # "check your key" and "you are out of credit" — and both are the user's to fix.
        from omnia.core.providers import ProviderError

        hub = _Hub(_Provider(fail=ProviderError("HTTP 401: invalid api key")))

        with pytest.raises(PhraseCheckError, match="invalid api key"):
            PhraseChecker(hub).check(PHRASE)

    def test_a_hub_that_cannot_build_one_says_so(self):
        hub = _Hub(fail=RuntimeError("no provider configured"))

        with pytest.raises(PhraseCheckError, match="no provider configured"):
            PhraseChecker(hub).check(PHRASE)

    def test_an_unexpected_error_does_not_leak_a_traceback_at_the_user(self):
        hub = _Hub(_Provider(fail=ValueError("some internal detail")))

        with pytest.raises(PhraseCheckError, match="see the Omnia log"):
            PhraseChecker(hub).check(PHRASE)
