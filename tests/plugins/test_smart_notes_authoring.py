"""Tests for smart_notes prompt authoring: the pure builders/parsers + PromptAuthor.

The LLM-touching behaviour follows the repo's contract pattern: one base class describing the
behaviour, two subclasses providing ``make_llm()`` — a FakeLLMProvider (always runs, free) and
the REAL configured provider (marked ``llm``, auto-skipped without credentials).
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, call_or_xfail, real_llm_provider_or_skip

from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.authoring import PromptAuthor
from omnia.plugins.smart_notes.authoring.author import (
    build_improve_prompt_message,
    build_improve_prompts_message,
    parse_improved_prompts,
)

# ---------------------------------------------------------------------------
# Pure message-build + reply-parse (no provider).
# ---------------------------------------------------------------------------


class TestImproveMessages:
    def test_single_message_carries_refs_target_and_rough_text(self):
        msg = build_improve_prompt_message(
            "Vocab", "Word", "Example", "vài câu ví dụ", ["Word", "Definition"]
        )
        assert "{{Word}}" in msg and "{{Definition}}" in msg
        assert "Example" in msg
        assert "vài câu ví dụ" in msg

    def test_single_message_handles_no_other_fields(self):
        msg = build_improve_prompt_message("V", "Word", "Word", "rough", [])
        assert "no other fields" in msg.lower()

    def test_batch_message_lists_each_field(self):
        msg = build_improve_prompts_message(
            "Vocab", "Word", [("Example", "ví dụ"), ("IPA", "phiên âm")]
        )
        assert "Example" in msg and "IPA" in msg
        assert "JSON" in msg


class TestParseImprovedPrompts:
    def test_parses_clean_json(self):
        out = parse_improved_prompts('{"Example": "Write {{Word}} examples"}')
        assert out == {"Example": "Write {{Word}} examples"}

    def test_drops_non_strings_and_blanks(self):
        out = parse_improved_prompts('{"A": "good", "B": 123, "C": "   "}')
        assert out == {"A": "good"}

    def test_tolerates_fences(self):
        out = parse_improved_prompts('```json\n{"A": "x"}\n```')
        assert out == {"A": "x"}

    def test_no_json_raises(self):
        with pytest.raises(ProviderError):
            parse_improved_prompts("I cannot help with that.")


# ---------------------------------------------------------------------------
# PromptAuthor: behaviour contract against fake + real LLMs.
# ---------------------------------------------------------------------------


class PromptAuthorContract:
    """Shared assertions for :class:`PromptAuthor`. Subclasses supply the LLM via make_llm()."""

    def make_llm(self):
        raise NotImplementedError

    def test_improve_returns_a_nonempty_prompt(self):
        author = PromptAuthor(self.make_llm())
        out = call_or_xfail(
            author.improve,
            note_type="Vocab",
            base_field="Word",
            target_field="Example",
            rough="make a couple of simple example sentences",
            other_fields=["Word", "Definition"],
        )
        assert isinstance(out, str) and out.strip()

    def test_improve_blank_rough_is_a_noop(self):
        # Blank input returns unchanged WITHOUT calling the model (true for fake and real).
        author = PromptAuthor(self.make_llm())
        assert (
            author.improve(
                note_type="V",
                base_field="Word",
                target_field="Example",
                rough="   ",
                other_fields=[],
            )
            == "   "
        )


class TestPromptAuthorFake(PromptAuthorContract):
    """The contract against a canned LLM — always runs, no quota."""

    def make_llm(self):
        return FakeLLMProvider(
            text="You are an expert. Write 2-3 examples for {{Word}}; bold it with <b>."
        )


@pytest.mark.llm
class TestPromptAuthorReal(PromptAuthorContract):
    """The contract against the REAL configured LLM (skips without credentials)."""

    def make_llm(self):
        return real_llm_provider_or_skip()


# ---------------------------------------------------------------------------
# PromptAuthor.auto_smart / improve_all with a canned (JSON-returning) fake.
# ---------------------------------------------------------------------------


class TestPromptAuthorWithCannedJson:
    def test_improve_all_parses_the_json_map(self):
        author = PromptAuthor(
            FakeLLMProvider(text='{"Example": "Polished {{Word}} prompt"}')
        )
        out = author.improve_all(
            note_type="Vocab", base_field="Word", items=[("Example", "rough")]
        )
        assert out == {"Example": "Polished {{Word}} prompt"}

    def test_improve_all_skips_blank_items_without_calling_llm(self):
        class _NoCall(FakeLLMProvider):
            def generate_text(self, *a, **k):
                raise AssertionError("must not call the LLM when nothing to improve")

        author = PromptAuthor(_NoCall())
        assert (
            author.improve_all(note_type="V", base_field="W", items=[("X", "   ")])
            == {}
        )
