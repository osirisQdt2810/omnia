"""Tests for the deterministic ``cloze`` tool (smart-notes tools phase 2).

Three things carry the tool's value and each has its own class here: the inflection matrix (it
must work in BOTH directions, since the de-inflector only walks inflected → base), markup
safety (a match may never break an HTML tag or a ``[sound:…]`` reference), and the
fall-through — a ``[cloze, ai]`` chain must reach the LLM ONLY when cloze declines, which is
the token saving the whole tools plan exists for. Pure logic — no Anki, no network.
"""

from __future__ import annotations

import logging

import pytest

from omnia.core.text import strip_markup
from omnia.plugins.smart_notes.config import (
    CompiledToolSpec,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine import GenerationService, compile_field_rule
from omnia.plugins.smart_notes.engine.rules import rule_prerequisites
from omnia.plugins.smart_notes.engine.tools import (
    ClozeRewriter,
    ClozeTool,
    GenerationPipeline,
    NotApplicable,
    Produced,
    ToolContext,
    ToolRequest,
    tools_catalog,
)

_CLOZE_OPEN = "{{c"


def _ctx() -> ToolContext:
    """A catalog/run context the deterministic tool never actually reads."""
    return ToolContext(
        providers=None, detector=None, logger=logging.getLogger("omnia.test")
    )


def _rule(**kwargs) -> SmartNotesFieldRule:
    """A compiled-looking rule with the cloze chain, defaulting to the Vocab shape."""
    params = kwargs.pop("params", {})
    base = {
        "target_field": "Cloze",
        "base_field": "Word",
        "kind": "text",
        "tools": (CompiledToolSpec(name="cloze", params=params),),
    }
    base.update(kwargs)
    return SmartNotesFieldRule(**base)


def _run(fields: dict[str, str], **kwargs):
    """Run the tool once and return its outcome."""
    rule = _rule(**kwargs)
    request = ToolRequest(
        rule=rule, fields=fields, params=ClozeTool.parse_params(rule.tools[0].params)
    )
    return ClozeTool().run(request, _ctx())


def _clozed(fields: dict[str, str], **kwargs) -> str:
    """Run the tool and return the produced text (fails the test if it declined)."""
    outcome = _run(fields, **kwargs)
    assert isinstance(outcome, Produced), outcome
    assert outcome.result.kind == "text"
    return outcome.result.text or ""


class TestClozeToolContract:
    """The registry-facing declarations the picker and pipeline read."""

    def test_is_a_deterministic_text_tool(self):
        assert ClozeTool.name == "cloze"
        assert ClozeTool.kinds == frozenset({"text"})
        assert ClozeTool.deterministic is True

    def test_is_in_the_catalog_with_a_params_schema(self):
        entry = {item["name"]: item for item in tools_catalog(_ctx())}["cloze"]
        assert entry["deterministic"] is True
        assert entry["kinds"] == ["text"]
        assert entry["unavailable_reason"] is None
        properties = entry["params_schema"]["properties"]
        assert set(properties) == {
            "sentence_field",
            "word_field",
            "match_word_forms",
            "separate_cards",
            "mask",
        }
        # The picker renders a dropdown from the enum, while the model itself stays a tolerant
        # str so a mask value from a newer release doesn't break the tool (ADR-010).
        assert properties["mask"]["enum"] == ["none", "hint_first_last"]

    def test_params_naming_fields_become_hard_prerequisites(self):
        rule = _rule(params={"sentence_field": "Sentence", "word_field": "Word"})
        assert rule_prerequisites(rule) == [("Sentence", "hard"), ("Word", "hard")]

    def test_unset_params_name_no_field(self):
        # The defaults resolve to the prompt's refs / the base field, both of which the rule
        # already accounts for — a blank param must not invent an edge onto "".
        assert ClozeTool.referenced_fields({}) == []
        assert ClozeTool.referenced_fields({"sentence_field": "  "}) == []

    def test_an_unknown_param_value_does_not_break_the_tool(self):
        # ADR-010: a newer release's mask mode loads and simply wraps without a hint.
        text = _clozed(
            {"Word": "survive", "Sentence": "They survive."},
            params={"sentence_field": "Sentence", "mask": "hint_syllables"},
        )
        assert text == "They {{c1::survive}}."


class TestInflectionMatrix:
    """Both directions, because ``word_variants`` only de-inflects INFLECTED → base.

    The headword field usually holds the lemma while the sentence inflects it, so the
    base-in-field / inflected-in-sentence row is the COMMON case and the one a naive
    ``words_boundary_pattern(word_variants(word))`` silently misses.
    """

    @pytest.mark.parametrize(
        ("word", "sentence", "expected"),
        [
            ("survive", "They survived the crash.", "They {{c1::survived}} the crash."),
            (
                "survived",
                "They survive every winter.",
                "They {{c1::survive}} every winter.",
            ),
            ("run", "She is running fast.", "She is {{c1::running}} fast."),
            ("running", "She likes to run.", "She likes to {{c1::run}}."),
            ("study", "He studies hard.", "He {{c1::studies}} hard."),
            ("stop", "The bus stopped here.", "The bus {{c1::stopped}} here."),
            ("happy", "She is happier now.", "She is {{c1::happier}} now."),
            ("go", "He goes home.", "He {{c1::goes}} home."),
        ],
    )
    def test_a_word_form_is_found_in_either_direction(self, word, sentence, expected):
        assert (
            _clozed(
                {"Word": word, "Sentence": sentence},
                params={"sentence_field": "Sentence"},
            )
            == expected
        )

    def test_the_surface_form_is_wrapped_never_the_lemma(self):
        text = _clozed(
            {"Word": "survive", "Sentence": "He survived."},
            params={"sentence_field": "Sentence"},
        )
        assert "{{c1::survived}}" in text and "survive}}" not in text

    def test_matching_is_case_insensitive_and_preserves_case(self):
        text = _clozed(
            {"Word": "survive", "Sentence": "Survived, he said."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "{{c1::Survived}}, he said."

    def test_word_forms_can_be_switched_off(self):
        outcome = _run(
            {"Word": "survive", "Sentence": "He survived."},
            params={"sentence_field": "Sentence", "match_word_forms": False},
        )
        assert isinstance(outcome, NotApplicable)

    def test_a_substring_is_never_a_match(self):
        outcome = _run(
            {"Word": "port", "Sentence": "An important report."},
            params={"sentence_field": "Sentence"},
        )
        assert isinstance(outcome, NotApplicable)

    def test_a_multi_word_headword_matches_as_a_phrase(self):
        text = _clozed(
            {"Word": "give up", "Sentence": "Don't give up now."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "Don't {{c1::give up}} now."


class TestMarkupSafety:
    """A match must never start inside markup, and the ORIGINAL value must come back intact."""

    def test_tags_around_the_match_survive(self):
        text = _clozed(
            {"Word": "cat", "Sentence": "The <b>cat</b> sat."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "The <b>{{c1::cat}}</b> sat."

    def test_a_match_never_spans_a_tag(self):
        # "sur<b>vived</b>" is one word on screen but two text runs in the field; wrapping it
        # would produce "{{c1::sur<b>vived}}</b>" — broken HTML and a broken cloze.
        outcome = _run(
            {"Word": "survive", "Sentence": "He sur<b>vived</b>."},
            params={"sentence_field": "Sentence"},
        )
        assert isinstance(outcome, NotApplicable)

    def test_a_word_inside_a_tags_attribute_is_not_matched(self):
        text = _clozed(
            {"Word": "cat", "Sentence": '<img src="cat.png"> A cat.'},
            params={"sentence_field": "Sentence"},
        )
        assert text == '<img src="cat.png"> A {{c1::cat}}.'

    def test_a_sound_reference_is_left_alone(self):
        text = _clozed(
            {"Word": "run", "Sentence": "[sound:run-away.mp3] I run."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "[sound:run-away.mp3] I {{c1::run}}."

    def test_an_entity_is_not_read_as_a_word(self):
        text = _clozed(
            {"Word": "nbsp", "Sentence": "a&nbsp;nbsp here"},
            params={"sentence_field": "Sentence"},
        )
        assert text == "a&nbsp;{{c1::nbsp}} here"

    def test_an_existing_cloze_is_not_nested(self):
        text = _clozed(
            {"Word": "cat", "Sentence": "The {{c1::cat}} and a cat."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "The {{c1::cat}} and a {{c1::cat}}."

    def test_the_word_field_may_itself_carry_markup(self):
        text = _clozed(
            {"Word": "<b>cat</b>[sound:cat.mp3]", "Sentence": "A cat."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "A {{c1::cat}}."


class TestMultipleOccurrences:
    def test_every_occurrence_is_hidden_on_one_card_by_default(self):
        # "ran" IS hidden now: the de-inflector carries an irregular table, so a form no suffix
        # rule can reach still resolves to the headword. (This assertion used to pin the
        # opposite as a documented limitation.)
        text = _clozed(
            {"Word": "run", "Sentence": "I run, you run, we ran."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "I {{c1::run}}, you {{c1::run}}, we {{c1::ran}}."

    def test_separate_cards_numbers_each_occurrence(self):
        text = _clozed(
            {"Word": "run", "Sentence": "I run, you run."},
            params={"sentence_field": "Sentence", "separate_cards": True},
        )
        assert text == "I {{c1::run}}, you {{c2::run}}."

    def test_numbering_continues_across_markup_runs(self):
        text = _clozed(
            {"Word": "cat", "Sentence": "<i>cat</i> and cat"},
            params={"sentence_field": "Sentence", "separate_cards": True},
        )
        assert text == "<i>{{c1::cat}}</i> and {{c2::cat}}"


class TestMask:
    def test_hint_shows_only_the_first_and_last_letter(self):
        text = _clozed(
            {"Word": "survive", "Sentence": "They survived."},
            params={"sentence_field": "Sentence", "mask": "hint_first_last"},
        )
        assert text == "They {{c1::survived::s______d}}."

    def test_a_two_letter_word_is_masked_completely(self):
        # "g_" would give the answer away, which is the one thing a hint must not do.
        text = _clozed(
            {"Word": "go", "Sentence": "I go home."},
            params={"sentence_field": "Sentence", "mask": "hint_first_last"},
        )
        assert text == "I {{c1::go::__}} home."


class TestDeclines:
    """Every decline is a fall-through, so its reason must name what it looked for."""

    def test_a_blank_sentence_declines(self):
        outcome = _run(
            {"Word": "cat", "Sentence": "  <br> "},
            params={"sentence_field": "Sentence"},
        )
        assert isinstance(outcome, NotApplicable)
        assert "Sentence" in outcome.reason

    def test_a_blank_word_declines(self):
        outcome = _run(
            {"Word": "", "Sentence": "A cat."}, params={"sentence_field": "Sentence"}
        )
        assert isinstance(outcome, NotApplicable)
        assert "Word" in outcome.reason

    def test_a_miss_names_the_word_and_the_field(self):
        outcome = _run(
            {"Word": "dog", "Sentence": "A cat sat."},
            params={"sentence_field": "Sentence"},
        )
        assert isinstance(outcome, NotApplicable)
        assert "'dog'" in outcome.reason and "'Sentence'" in outcome.reason


class TestFieldResolution:
    """Where the sentence and the word come from when the params leave it to the rule."""

    def test_the_sentence_defaults_to_the_first_prompt_reference(self):
        text = _clozed(
            {"Word": "cat", "Sentence": "A cat."},
            prompt="Cloze the word in {{Sentence}}",
        )
        assert text == "A {{c1::cat}}."

    def test_the_word_defaults_to_the_note_types_base_field(self):
        # No word_field param: the compiled rule's base_field is what makes this resolvable.
        rule = compile_field_rule(
            SmartNotesFieldConfig(
                field="Cloze",
                enabled=True,
                type="text",
                prompt="cloze {{Sentence}}",
                tools=[FieldToolConfig(tool="cloze")],
            ),
            "Word",
        )
        assert rule.base_field == "Word"
        request = ToolRequest(
            rule=rule,
            fields={"Word": "cat", "Sentence": "A cat."},
            params=ClozeTool.parse_params(rule.tools[0].params),
        )
        outcome = ClozeTool().run(request, _ctx())
        assert isinstance(outcome, Produced)
        assert outcome.result.text == "A {{c1::cat}}."

    def test_a_field_name_is_matched_case_insensitively(self):
        text = _clozed(
            {"Word": "cat", "Sentence": "A cat."},
            params={"sentence_field": "sentence", "word_field": "word"},
        )
        assert text == "A {{c1::cat}}."


class TestClozeIsReversible:
    """Property: stripping the wrappers off the output gives the input back, verbatim."""

    @pytest.mark.parametrize(
        "sentence",
        [
            "The cat sat on the mat.",
            "A <b>cat</b> and a cat, plus [sound:cat.mp3].",
            "Cats! CAT? cat…",
            "<div>the cat</div><div>another cat</div>",
            "a&nbsp;cat &amp; a cat",
        ],
    )
    @pytest.mark.parametrize("separate_cards", [False, True])
    @pytest.mark.parametrize("mask", ["none", "hint_first_last"])
    def test_unwrapping_the_output_returns_the_input(
        self, sentence, separate_cards, mask
    ):
        clozed = ClozeRewriter("cat", separate_cards=separate_cards, mask=mask).rewrite(
            sentence
        )
        assert clozed is not None
        assert _CLOZE_OPEN in clozed
        # strip_markup unwraps a cloze to its ANSWER (dropping any hint), so the two texts must
        # agree once both sides go through it — that is the invariant: only wrappers were added.
        assert strip_markup(clozed) == strip_markup(sentence)


class _CountingLLM:
    """A fake LLM that records every call — the assertion target of the fall-through test."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        self.prompts.append(prompt)
        return f"llm:{prompt}"


class _CountingHub:
    """A ProviderHub-shaped stub handing out the one counting LLM."""

    def __init__(self, llm: _CountingLLM) -> None:
        self._llm = llm

    def llm(self, **kwargs):
        return self._llm

    def tts(self, **kwargs):  # pragma: no cover - no tts rule in these tests
        raise AssertionError("no tts call expected")


def _cloze_then_ai_config() -> SmartNotesNoteTypeConfig:
    return SmartNotesNoteTypeConfig(
        note_type="Vocab",
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Cloze",
                enabled=True,
                type="text",
                prompt="Cloze {{Word}} in {{Sentence}}",
                tools=[
                    FieldToolConfig(
                        tool="cloze", params={"sentence_field": "Sentence"}
                    ),
                    FieldToolConfig(tool="ai"),
                ],
            )
        ],
    )


class TestClozeThenAiChain:
    """The point of the whole plan: the LLM runs ONLY when the deterministic tool declines."""

    def test_a_hit_never_reaches_the_llm(self):
        llm = _CountingLLM()
        service = GenerationService(_CountingHub(llm))

        results, _blocked, failed = service.generate_note(
            _cloze_then_ai_config(),
            {"Word": "survive", "Sentence": "They survived.", "Cloze": ""},
        )

        assert failed == []
        assert [rule.target_field for rule, _ in results] == ["Cloze"]
        assert results[0][1].text == "They {{c1::survived}}."
        assert llm.prompts == []  # the token saving this plan exists for

    def test_a_miss_falls_through_to_the_llm(self):
        llm = _CountingLLM()
        service = GenerationService(_CountingHub(llm))

        results, _blocked, failed = service.generate_note(
            _cloze_then_ai_config(),
            {"Word": "dog", "Sentence": "A cat sat.", "Cloze": ""},
        )

        assert failed == []
        assert llm.prompts == ["Cloze dog in A cat sat."]
        assert results[0][1].text.startswith("llm:")

    def test_the_winning_tool_is_stamped_on_the_result(self):
        # Provenance is what lets the batch summary count fields that fell back to a later tool.
        rule = compile_field_rule(_cloze_then_ai_config().fields[0], "Word")
        pipeline = GenerationPipeline(
            ToolContext(
                providers=_CountingHub(_CountingLLM()),
                detector=None,
                logger=logging.getLogger("omnia.test"),
            )
        )

        hit = pipeline.run(rule, {"Word": "cat", "Sentence": "A cat."})
        miss = pipeline.run(rule, {"Word": "dog", "Sentence": "A cat."})

        assert hit.produced.tool == "cloze"
        assert miss.produced.tool == "ai"


class TestTheChainIsNotStoppedByABadRewrite:
    """Three ways the tool used to Produce wrong output — which stops the chain.

    A wrong `Produced` is worse than a miss: the chain ends, `ai` never gets to correct it, and
    the bad text is written to the note. All three of these now decline (or simply do not match)
    so the fallback still runs.
    """

    def test_a_speculative_stem_of_the_word_is_not_clozed(self):
        # word_variants("toes") offers the stem "to" — fine for widening an Anki search, ruinous
        # compiled into a rewrite. Same shape for bees->be, ones->on, uses->us.
        text = _clozed(
            {"Word": "toes", "Sentence": "I stubbed my toes on the way to work."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "I stubbed my {{c1::toes}} on the way to work."

    def test_a_headword_that_is_itself_a_function_word_still_clozes(self):
        # The filter above must never silence the user's own word.
        text = _clozed(
            {"Word": "to", "Sentence": "I want to go."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "I want {{c1::to}} go."

    def test_a_real_short_base_is_still_reachable_through_a_token(self):
        # "goes" -> "go" is the same shape as "toes" -> "to"; only the word list separates them.
        text = _clozed(
            {"Word": "goes", "Sentence": "He goes and they go."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "He {{c1::goes}} and they {{c1::go}}."

    def test_a_word_split_by_a_tag_is_not_clozed_as_a_fragment(self):
        # finditer(value, start, end) treats end as a truncation, so \b matched at the span's
        # edge and hid "run" out of "<b>run</b>ning", leaving a card whose answer is a fragment.
        assert isinstance(
            _run(
                {"Word": "run", "Sentence": "She was <b>run</b>ning fast."},
                params={"sentence_field": "Sentence"},
            ),
            NotApplicable,
        )

    def test_the_same_field_cannot_be_both_the_sentence_and_the_word(self):
        # Both params default independently and can land on the same field; clozing a word
        # inside itself yields "{{c1::word}}", which is not a card.
        assert isinstance(
            _run(
                {"Word": "cat"},
                params={"sentence_field": "Word", "word_field": "Word"},
            ),
            NotApplicable,
        )


class TestIrregularFormsMeetTheFunctionWordFilter:
    """Where the irregular table and the speculative-stem filter overlap.

    Both landed for the same reason — a cloze that silently rewrites the wrong words is worse
    than one that declines — and they pull in opposite directions on the same short words, so
    the boundary between them is pinned here rather than left to chance.
    """

    @pytest.mark.parametrize(
        ("word", "sentence", "expected"),
        [
            (
                "run",
                "I run now, we ran then.",
                "I {{c1::run}} now, we {{c1::ran}} then.",
            ),
            ("eat", "They ate it.", "They {{c1::ate}} it."),
            ("go", "She went home.", "She {{c1::went}} home."),
            ("child", "The children left.", "The {{c1::children}} left."),
            ("good", "This is better.", "This is {{c1::better}}."),
        ],
    )
    def test_an_irregular_form_is_clozed(self, word, sentence, expected):
        assert (
            _clozed(
                {"Word": word, "Sentence": sentence},
                params={"sentence_field": "Sentence"},
            )
            == expected
        )

    def test_a_function_word_headword_still_reaches_its_own_irregulars(self):
        # "be" is in the filter list, but it is also this card's headword — the exemption is
        # what lets a beginner's "be" card hide the "was" in its example.
        text = _clozed(
            {"Word": "be", "Sentence": "He was happy and will be fine."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "He {{c1::was}} happy and will {{c1::be}} fine."

    def test_the_table_does_not_reopen_the_speculative_stem_hole(self):
        # "bees" strips to the stem "be", which the table now also knows as a real base. The
        # filter still has to keep the sentence's "be" out of a "bees" card.
        text = _clozed(
            {"Word": "bees", "Sentence": "The bees can be loud."},
            params={"sentence_field": "Sentence"},
        )
        assert text == "The {{c1::bees}} can be loud."
