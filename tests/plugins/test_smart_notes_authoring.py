"""Tests for smart_notes prompt authoring: the pure builders/parsers + PromptAuthor.

The LLM-touching behaviour follows the repo's contract pattern: one base class describing the
behaviour, two subclasses providing ``make_llm()`` — a FakeLLMProvider (always runs, free) and
the REAL configured provider (marked ``llm``, auto-skipped without credentials).
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, call_or_xfail, real_llm_provider_or_skip

from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.authoring import (
    EdgeChange,
    EdgeKinding,
    PromptAuthor,
)
from omnia.plugins.smart_notes.authoring.author import (
    build_classify_deps_message,
    build_edge_change_message,
    build_improve_in_popover_message,
    build_improve_prompt_message,
    build_improve_prompts_message,
    parse_classified_deps,
    parse_improved_prompts,
)
from omnia.plugins.smart_notes.config import FieldDep
from omnia.plugins.smart_notes.engine.rules import reconcile_field_deps


class _ScriptedLLM(FakeLLMProvider):
    """A fake LLM that returns successive canned replies and records each call.

    The first ``generate_text`` returns ``replies[0]``, the second ``replies[1]``, and so on
    (the last reply repeats if asked again). Each call's ``(prompt, system, temperature)`` is
    recorded so tests can assert the guard rail's retry behaviour.
    """

    def __init__(self, replies: list[str]) -> None:
        super().__init__()
        self._replies = replies
        self.calls: list[tuple[str, object, object]] = []

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        self.calls.append((prompt, system, temperature))
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[index]


class _RaisingLLM(FakeLLMProvider):
    """A fake LLM that fails the test if it is ever called (proves a no-LLM-call path)."""

    def generate_text(self, *a, **k):
        raise AssertionError("the LLM must not be called on this path")


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


# ---------------------------------------------------------------------------
# Two-way sync: parse_classified_deps (pure, tolerant).
# ---------------------------------------------------------------------------


class TestParseClassifiedDeps:
    def test_parses_wrapper_key(self):
        out = parse_classified_deps(
            '{"dependencies": [{"field": "Word", "kind": "hard", "reason": "core"}]}'
        )
        assert out == (EdgeKinding(field="Word", kind="hard", reason="core"),)

    def test_parses_bare_array_fallback(self):
        out = parse_classified_deps('[{"field": "Word", "kind": "soft"}]')
        assert out == (EdgeKinding(field="Word", kind="soft"),)

    def test_wrapper_object_with_inner_array_and_fences(self):
        # Realistic LLM drift: fenced wrapper object whose value is an inner array. The
        # precedence guard must take the wrapper path and not choke on the inner "[".
        raw = (
            'Sure:\n```json\n{"dependencies": [{"field": "Word", "kind": "soft"}]}\n```'
        )
        assert parse_classified_deps(raw) == (EdgeKinding(field="Word", kind="soft"),)

    def test_malformed_kind_defaults_to_hard(self):
        out = parse_classified_deps(
            '{"dependencies": [{"field": "Word", "kind": "??"}]}'
        )
        assert out == (EdgeKinding(field="Word", kind="hard"),)

    def test_missing_field_is_dropped(self):
        out = parse_classified_deps(
            '{"dependencies": [{"kind": "hard"}, {"field": "Word", "kind": "soft"}]}'
        )
        assert out == (EdgeKinding(field="Word", kind="soft"),)

    def test_case_insensitive_dedup_keeps_first(self):
        out = parse_classified_deps(
            '{"dependencies": [{"field": "Word", "kind": "hard"}, '
            '{"field": "word", "kind": "soft"}]}'
        )
        assert out == (EdgeKinding(field="Word", kind="hard"),)

    def test_no_json_raises(self):
        with pytest.raises(ProviderError):
            parse_classified_deps("sorry, I can't")


# ---------------------------------------------------------------------------
# Two-way sync: classify_dependencies (no LLM call when there are no refs).
# ---------------------------------------------------------------------------


class TestClassifyDependencies:
    def test_empty_refs_returns_nothing_without_calling_llm(self):
        author = PromptAuthor(_RaisingLLM())
        out = author.classify_dependencies(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            prompt="Write an example.",
            refs=[],
        )
        assert out == ()

    def test_classifies_each_ref(self):
        author = PromptAuthor(
            _ScriptedLLM(['{"dependencies": [{"field": "Kanji", "kind": "hard"}]}'])
        )
        out = author.classify_dependencies(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            prompt="Use {{Kanji}}.",
            refs=["Kanji"],
        )
        assert out == (EdgeKinding(field="Kanji", kind="hard"),)


# ---------------------------------------------------------------------------
# Two-way sync: reconcile_field_deps (pure, deterministic — B1/B2).
# ---------------------------------------------------------------------------


class TestReconcileFieldDeps:
    def test_new_ref_gets_classified_kind_as_auto(self):
        out = reconcile_field_deps(
            "Use {{Reading}} for the example.",
            {"Reading": "soft"},
            [],
        )
        assert out == [FieldDep(field="Reading", kind="soft", auto=True)]

    def test_existing_ref_is_kept_unchanged(self):
        # The user already set Reading=hard; even though the classifier says soft, B2 keeps it.
        existing = [FieldDep(field="Reading", kind="hard", auto=False)]
        out = reconcile_field_deps("Use {{Reading}}.", {"Reading": "soft"}, existing)
        assert out == existing

    def test_auto_edge_whose_ref_vanished_is_dropped(self):
        out = reconcile_field_deps(
            "No references here.",
            {},
            [FieldDep(field="Reading", kind="soft", auto=True)],
        )
        assert out == []

    def test_user_edge_whose_ref_vanished_is_kept(self):
        user_edge = [FieldDep(field="Reading", kind="hard", auto=False)]
        out = reconcile_field_deps("No references here.", {}, user_edge)
        assert out == user_edge

    def test_explicit_only_user_edge_kept_with_no_refs(self):
        # A user edge with no matching {{ref}} (explicit-only) survives.
        user_edge = [FieldDep(field="Meaning", kind="soft", auto=False)]
        out = reconcile_field_deps("plain prompt", {}, user_edge)
        assert out == user_edge

    def test_classified_missing_kind_defaults_hard(self):
        out = reconcile_field_deps("Use {{Kanji}}.", {}, [])
        assert out == [FieldDep(field="Kanji", kind="hard", auto=True)]


# ---------------------------------------------------------------------------
# Two-way sync: rewrite_for_edge_change + improve_in_popover (guard rail).
# ---------------------------------------------------------------------------

_KANJI_FIELDS = ["Kanji", "Reading", "Example"]


class TestRewriteForEdgeChange:
    def test_consistent_rewrite_is_accepted(self):
        author = PromptAuthor(
            _ScriptedLLM(["Write a sentence using {{Kanji}} in context."])
        )
        out = author.rewrite_for_edge_change(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            old_prompt="Write a sentence with {{Kanji}}.",
            kept_deps=[FieldDep(field="Kanji", kind="hard")],
            change=EdgeChange(
                action="toggle", src="Kanji", old_kind="soft", new_kind="hard"
            ),
            known_fields=_KANJI_FIELDS,
            intended_depends_on=[FieldDep(field="Kanji", kind="hard")],
        )
        assert out.ok is True
        assert "{{Kanji}}" in out.prompt

    def test_dropping_a_kept_ref_retries_then_fails_with_old_prompt(self):
        old = "Write a sentence with {{Kanji}}."
        # Both attempts drop the required {{Kanji}} ref → gate fails twice.
        llm = _ScriptedLLM(["Write a generic sentence.", "Still no reference."])
        author = PromptAuthor(llm)
        out = author.rewrite_for_edge_change(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            old_prompt=old,
            kept_deps=[FieldDep(field="Kanji", kind="hard")],
            change=EdgeChange(action="add", src="Kanji", new_kind="hard"),
            known_fields=_KANJI_FIELDS,
            intended_depends_on=[FieldDep(field="Kanji", kind="hard")],
        )
        assert out.ok is False
        assert out.prompt == old
        assert len(llm.calls) == 2  # one initial attempt + one bounded repair retry

    def test_soft_add_passes_the_gate_against_intended_soft_deps(self):
        # A SOFT add: the intended deps carry kind=soft. The rewrite references {{Reading}};
        # the gate must NOT falsely fail because a bare derived ref defaults to hard.
        author = PromptAuthor(
            _ScriptedLLM(
                ["Write a sentence with {{Kanji}}; if {{Reading}} is present, use it."]
            )
        )
        out = author.rewrite_for_edge_change(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            old_prompt="Write a sentence with {{Kanji}}.",
            kept_deps=[FieldDep(field="Kanji", kind="hard")],
            change=EdgeChange(action="add", src="Reading", new_kind="soft"),
            known_fields=_KANJI_FIELDS,
            intended_depends_on=[
                FieldDep(field="Kanji", kind="hard"),
                FieldDep(field="Reading", kind="soft"),
            ],
        )
        assert out.ok is True
        assert "{{Reading}}" in out.prompt

    def test_retry_then_succeeds_on_second_attempt(self):
        # First attempt drops {{Kanji}} (fails the gate); the bounded repair retry references
        # it and passes — proving the retry path can recover, not only fail.
        llm = _ScriptedLLM(["A generic sentence.", "Write a sentence using {{Kanji}}."])
        author = PromptAuthor(llm)
        out = author.rewrite_for_edge_change(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            old_prompt="Write a sentence with {{Kanji}}.",
            kept_deps=[FieldDep(field="Kanji", kind="hard")],
            change=EdgeChange(action="add", src="Kanji", new_kind="hard"),
            known_fields=_KANJI_FIELDS,
            intended_depends_on=[FieldDep(field="Kanji", kind="hard")],
        )
        assert out.ok is True
        assert "{{Kanji}}" in out.prompt
        assert len(llm.calls) == 2  # initial fail + one repair retry that succeeds


class TestImproveInPopover:
    def test_pinned_rewrite_that_adds_a_ref_fails(self):
        old = "Write a sentence using {{Kanji}}."
        # The rewrite sneaks in a new {{Reading}} ref → must fail the pinned gate.
        author = PromptAuthor(
            _ScriptedLLM(
                [
                    "Write using {{Kanji}} and {{Reading}}.",
                    "Write using {{Kanji}} and {{Reading}} again.",
                ]
            )
        )
        out = author.improve_in_popover(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            prompt=old,
            fixed_deps=[FieldDep(field="Kanji", kind="hard")],
            known_fields=_KANJI_FIELDS,
        )
        assert out.ok is False
        assert out.prompt == old

    def test_pinned_rewrite_keeping_the_dep_set_is_accepted(self):
        author = PromptAuthor(
            _ScriptedLLM(["Carefully write a sentence using {{Kanji}}."])
        )
        out = author.improve_in_popover(
            note_type="Kanji",
            base_field="Kanji",
            target_field="Example",
            prompt="Write using {{Kanji}}.",
            fixed_deps=[FieldDep(field="Kanji", kind="hard")],
            known_fields=_KANJI_FIELDS,
        )
        assert out.ok is True
        assert "{{Kanji}}" in out.prompt


# ---------------------------------------------------------------------------
# Two-way sync: message builders are field-name-agnostic (no vocab leak).
# ---------------------------------------------------------------------------

# Words specific to the vocab domain that must never appear as hard-coded RULES in a generic
# builder (the builders are reused across Kanji, Chemistry, etc.).
_VOCAB_LEAK_WORDS = ("definition", "headword", "ipa", "pronunciation", "vocabulary")


def _assert_no_vocab_leak(message: str) -> None:
    lowered = message.lower()
    leaked = [word for word in _VOCAB_LEAK_WORDS if word in lowered]
    assert not leaked, f"vocab-specific words leaked into a generic builder: {leaked}"


class TestMessageBuildersAreGeneric:
    def test_classify_message_for_chemistry_note_type(self):
        msg = build_classify_deps_message(
            "Chemistry",
            "Compound",
            "Reaction",
            "Describe the reaction of {{Compound}} using {{Catalyst}}.",
            ["Compound", "Catalyst"],
        )
        assert "Chemistry" in msg and "{{Compound}}" in msg
        assert "Compound" in msg and "Catalyst" in msg
        _assert_no_vocab_leak(msg)

    def test_edge_change_message_for_chemistry_note_type(self):
        msg = build_edge_change_message(
            "Chemistry",
            "Compound",
            "Reaction",
            "Describe the reaction of {{Compound}}.",
            [FieldDep(field="Compound", kind="hard")],
            EdgeChange(action="add", src="Catalyst", new_kind="soft"),
        )
        assert "{{Catalyst}}" in msg and "{{Compound}}" in msg
        _assert_no_vocab_leak(msg)

    def test_improve_in_popover_message_for_chemistry_note_type(self):
        msg = build_improve_in_popover_message(
            "Chemistry",
            "Compound",
            "Reaction",
            "Describe the reaction of {{Compound}}.",
            [FieldDep(field="Compound", kind="hard")],
        )
        assert "{{Compound}}" in msg
        _assert_no_vocab_leak(msg)
