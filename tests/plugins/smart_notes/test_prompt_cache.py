"""LAYER 2, engine side: a text rule's prompt is SPLIT, never rewritten.

The whole safety argument for prompt caching is that the split is lossless: what the model
receives is byte-for-byte what it received before, so a provider that cannot cache cannot
behave differently, and no existing user's output can change. These tests assert that rather
than assert *about* it — at the pure function (:func:`split_prompt`), at the rule seam
(:func:`prompt_parts_for`) and at the generator that calls the provider.
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider

from omnia.core.providers.llm.base import PromptParts
from omnia.plugins.smart_notes.config import SmartNotesFieldRule
from omnia.plugins.smart_notes.engine import GenerationService
from omnia.plugins.smart_notes.engine.interpolation import interpolate, split_prompt
from omnia.plugins.smart_notes.engine.rules import prompt_for, prompt_parts_for

# Templates chosen for the ways a split can go wrong, not for variety: no ref at all, a ref in
# the middle, a LEADING ref (empty prefix), a repeated ref, a cloze deletion (never a ref), a
# ref that resolves to nothing, and a ref adjacent to another with no literal between them.
_TEMPLATES = (
    "",
    "Write a definition.",
    "Define {{Word}} in one line.",
    "{{Word}} — define it.",
    "Define {{Word}}; use {{Word}} in a sentence.",
    "{{c1::hidden}} explain {{Word}}",
    "Explain {{Missing}} thoroughly.",
    "Explain {{Word}}{{Hint}}.",
    "Explain {{ Word }} (padded braces).",
)

_FIELDS = {"Word": "cat", "Hint": "feline", "Sentence": "The cat sat."}


class _SpyLLM(FakeLLMProvider):
    """Records the prompt that actually reached the provider through the base's default path."""

    def __init__(self) -> None:
        super().__init__(text="generated")
        self.prompts: list[str] = []
        self.parts: list[PromptParts] = []

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        self.prompts.append(prompt)
        return self._text

    def generate_cached_text(
        self, parts, *, system=None, temperature=None, max_tokens=None
    ):
        self.parts.append(parts)
        return super().generate_cached_text(
            parts, system=system, temperature=temperature, max_tokens=max_tokens
        )


class _StubHub:
    """A ProviderHub-shaped stub handing out one LLM."""

    def __init__(self, llm) -> None:
        self._llm = llm

    def llm(self, *, model="", image_model="", provider=""):
        return self._llm

    def tts(self, *, provider=""):
        return FakeTTSProvider()

    def resolve_auto_voice(self, lang, *, reason=""):
        return ("fake", "voice")


class TestSplitPromptIsLossless:
    """``prefix + suffix`` must equal ``interpolate`` exactly, for every template shape."""

    @pytest.mark.parametrize("template", _TEMPLATES)
    def test_the_two_parts_rejoin_to_exactly_what_interpolate_returns(self, template):
        assert split_prompt(template, _FIELDS).joined() == interpolate(
            template, _FIELDS
        )

    @pytest.mark.parametrize("template", _TEMPLATES)
    def test_the_prefix_never_contains_a_substituted_value(self, template):
        # The prefix is the literal head, so it is identical for every note of the note type —
        # which is the only property that makes it cacheable at all.
        prefix = split_prompt(template, _FIELDS).prefix
        assert (
            prefix == split_prompt(template, {"Word": "dog", "Hint": "canine"}).prefix
        )

    def test_the_prefix_is_the_literal_head_before_the_first_ref(self):
        parts = split_prompt("Define {{Word}} in one line.", _FIELDS)
        assert parts.prefix == "Define "
        assert parts.suffix == "cat in one line."

    def test_a_template_that_leads_with_a_ref_gets_no_cacheable_prefix(self):
        # The accepted cost of never touching the string the model reads: such a template gets
        # no benefit. It must still be lossless.
        parts = split_prompt("{{Word}} — define it.", _FIELDS)
        assert parts.prefix == ""
        assert parts.joined() == "cat — define it."

    def test_a_template_with_no_ref_is_all_prefix(self):
        parts = split_prompt("Write a definition.", _FIELDS)
        assert (parts.prefix, parts.suffix) == ("Write a definition.", "")

    def test_a_cloze_deletion_is_not_a_ref_and_does_not_split(self):
        # {{c1::…}} is Anki syntax, never a field reference — splitting there would put a live
        # cloze marker in a "stable" prefix and interpolate nothing.
        parts = split_prompt("{{c1::hidden}} explain {{Word}}", _FIELDS)
        assert parts.prefix == "{{c1::hidden}} explain "
        assert parts.joined() == "{{c1::hidden}} explain cat"


class TestPromptPartsForMatchesPromptFor:
    """The rule seam must agree with the un-split function it replaces, branch for branch."""

    @pytest.mark.parametrize("template", _TEMPLATES)
    def test_joining_the_parts_gives_back_prompt_for(self, template):
        rule = SmartNotesFieldRule(
            kind="text", prompt=template, target_field="Definition"
        )
        assert prompt_parts_for(rule, _FIELDS).joined() == prompt_for(rule, _FIELDS)

    def test_a_rule_with_no_template_puts_the_source_value_in_the_suffix(self):
        # Nothing repeats across notes here — the whole prompt IS one note's value — so there
        # is deliberately no prefix to offer.
        rule = SmartNotesFieldRule(
            kind="text", prompt="", source_field="Sentence", target_field="Definition"
        )
        parts = prompt_parts_for(rule, _FIELDS)
        assert (parts.prefix, parts.suffix) == ("", "The cat sat.")
        assert parts.joined() == prompt_for(rule, _FIELDS)


class TestTheTextGeneratorUsesTheCachedSeam:
    def test_the_provider_receives_the_template_head_as_the_prefix(self):
        llm = _SpyLLM()
        rule = SmartNotesFieldRule(
            kind="text", prompt="Define {{Word}} briefly.", target_field="Definition"
        )
        GenerationService(_StubHub(llm)).generate(rule, {"Word": "cat"})
        assert [p.prefix for p in llm.parts] == ["Define "]

    def test_a_provider_that_cannot_cache_receives_the_prompt_it_always_received(self):
        # "A provider that cannot cache must behave EXACTLY as today", asserted at the seam:
        # the string reaching generate_text is still prompt_for's output, unchanged.
        llm = _SpyLLM()
        rule = SmartNotesFieldRule(
            kind="text", prompt="Define {{Word}} briefly.", target_field="Definition"
        )
        GenerationService(_StubHub(llm)).generate(rule, {"Word": "cat"})
        assert llm.prompts == [prompt_for(rule, {"Word": "cat"})]

    def test_a_fake_with_no_override_at_all_still_generates(self):
        # The default inherited from LLMProvider is the only thing keeping every existing fake
        # (and every provider nobody updated) working.
        rule = SmartNotesFieldRule(
            kind="text", prompt="Define {{Word}}", target_field="Definition"
        )
        result = GenerationService(_StubHub(FakeLLMProvider(text="a feline"))).generate(
            rule, {"Word": "cat"}
        )
        assert result.text == "a feline"
