"""Tests for smart_notes (the note-type-centric model, the engine, and auto-smart).

This file also holds the real-provider feature tests: smart_notes' GenerationService driven
by EACH real provider, marked ``llm``/``tts`` and auto-skipping without credentials.
"""

from __future__ import annotations

import base64

import pytest
from conftest import (
    FakeHttpClient,
    FakeLLMProvider,
    FakeTTSProvider,
    assert_valid_audio,
    call_or_xfail,
    is_provider_limit_error,
    llm_provider_params,
    real_llm_provider_for_or_skip,
    real_tts_provider_for_or_skip,
    tts_provider_params,
)

from omnia.core.config.models import (
    LLMSettings,
    OpenAICompatibleLLMSettings,
    TTSSettings,
)
from omnia.core.providers import ProviderError, ProviderHub
from omnia.plugins.smart_notes.authoring import (
    AutoSmartDep,
    AutoSmartField,
    PromptAuthor,
    apply_auto_smart,
    build_auto_smart_prompt,
    candidate_fields,
    parse_auto_smart_response,
)
from omnia.plugins.smart_notes.config import (
    FieldDep,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import (
    GenerationService,
    SmartNotesCycleError,
    applies_to_deck,
    chunk,
    compile_note_type_rules,
    convert_markdown_to_html,
    dedupe_preserving_order,
    extract_field_refs,
    interpolate,
    order_rules,
    should_skip_rule,
)

# ---------------------------------------------------------------------------
# Mocked / offline tests
# ---------------------------------------------------------------------------


class TestPromptInterpolation:
    def test_extract_field_refs_skips_cloze(self):
        assert extract_field_refs("Define {{Word}} using {{Hint}}") == ["Word", "Hint"]
        assert extract_field_refs("{{c1::hidden}} {{Word}}") == ["Word"]

    def test_interpolate_substitutes_and_skips_cloze(self):
        out = interpolate("{{c1::x}} define {{Word}}", {"Word": "cat"})
        assert out == "{{c1::x}} define cat"
        assert interpolate("{{Missing}}!", {}) == "!"


class TestSmartNotesModel:
    def test_empty_settings_validates(self):
        # A fresh, empty config must load (smart_notes ships disabled with no note types).
        settings = SmartNotesSettings()
        assert settings.note_types == []
        assert settings.allow_empty_fields is False
        assert settings.regenerate_when_batching is True
        assert settings.generate_at_review is False

    def test_populated_settings_round_trips(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Meaning", enabled=True, type="text", prompt="Define {{Word}}"
                ),
                SmartNotesFieldConfig(field="Audio", enabled=True, type="tts"),
            ],
        )
        settings = SmartNotesSettings(note_types=[config])
        rebuilt = SmartNotesSettings(**settings.dict())
        assert rebuilt == settings
        assert rebuilt.note_type_config("Basic").base_field == "Word"

    def test_field_type_is_validated(self):
        with pytest.raises(ValueError):
            SmartNotesFieldConfig(field="X", type="video")

    def test_note_type_config_lookup_returns_none_when_absent(self):
        settings = SmartNotesSettings(
            note_types=[SmartNotesNoteTypeConfig(note_type="Basic", base_field="W")]
        )
        assert settings.note_type_config("Basic") is not None
        assert settings.note_type_config("Cloze") is None

    def test_generatable_fields_excludes_base_and_disabled(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(field="Word", enabled=True),  # base — excluded
                SmartNotesFieldConfig(field="Meaning", enabled=True),
                SmartNotesFieldConfig(
                    field="Off", enabled=False
                ),  # disabled — excluded
            ],
        )
        assert [f.field for f in config.generatable_fields()] == ["Meaning"]

    def test_decks_defaults_empty_and_accepts_ids(self):
        default = SmartNotesNoteTypeConfig(note_type="Basic", base_field="W")
        assert default.decks == []
        scoped = SmartNotesNoteTypeConfig(
            note_type="Basic", base_field="W", decks=[10, 20]
        )
        assert scoped.decks == [10, 20]


class TestFieldDepModel:
    def test_kind_defaults_to_hard(self):
        assert FieldDep(field="Word").kind == "hard"

    def test_kind_enum_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            FieldDep(field="Word", kind="maybe")

    def test_accepts_soft(self):
        assert FieldDep(field="Word", kind="soft").kind == "soft"

    def test_round_trips_through_field_config(self):
        config = SmartNotesFieldConfig(
            field="Def",
            enabled=True,
            depends_on=[FieldDep(field="Word", kind="soft")],
        )
        rebuilt = SmartNotesFieldConfig(**config.dict())
        assert rebuilt == config
        assert rebuilt.depends_on[0].kind == "soft"

    def test_model_layer_does_not_reject_self_or_unknown_reference(self):
        # A field may depend on itself or a not-yet-created field at the model layer;
        # whole-note-type cycle/presence checks live in the engine, not here.
        config = SmartNotesFieldConfig(
            field="Def",
            enabled=True,
            depends_on=[FieldDep(field="Def"), FieldDep(field="Ghost")],
        )
        assert [d.field for d in config.depends_on] == ["Def", "Ghost"]


class TestAppliesToDeck:
    def test_empty_decks_apply_to_any_deck(self):
        config = SmartNotesNoteTypeConfig(note_type="Basic", base_field="W")
        assert applies_to_deck(config, 1) is True
        assert applies_to_deck(config, 999) is True

    def test_scoped_config_applies_only_to_listed_decks(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic", base_field="W", decks=[1, 2]
        )
        assert applies_to_deck(config, 1) is True
        assert applies_to_deck(config, 2) is True
        assert applies_to_deck(config, 3) is False


class TestCompileNoteTypeRules:
    def test_compiles_one_rule_per_generatable_field(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Meaning", enabled=True, type="text", prompt="Define {{Word}}"
                ),
                SmartNotesFieldConfig(field="Audio", enabled=True, type="tts"),
                SmartNotesFieldConfig(field="Off", enabled=False),
            ],
        )
        rules = compile_note_type_rules(config)
        assert [r.target_field for r in rules] == ["Meaning", "Audio"]
        meaning = rules[0]
        assert meaning.kind == "text"
        assert meaning.prompt == "Define {{Word}}"
        # No source_field needed when a prompt is given.
        assert meaning.source_field == ""

    def test_bare_field_falls_back_to_base_as_source(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[SmartNotesFieldConfig(field="Echo", enabled=True, type="text")],
        )
        rule = compile_note_type_rules(config)[0]
        assert rule.prompt == ""
        assert rule.source_field == "Word"

    def test_carries_overrides_and_overwrite(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Meaning",
                    enabled=True,
                    type="text",
                    prompt="x",
                    provider="gemini",
                    model="m",
                    voice="v",
                    overwrite=True,
                )
            ],
        )
        rule = compile_note_type_rules(config)[0]
        assert (rule.provider, rule.model, rule.voice, rule.overwrite) == (
            "gemini",
            "m",
            "v",
            True,
        )


class TestBatchPlanningHelpers:
    def test_dedupe_preserves_first_seen_order(self):
        assert dedupe_preserving_order([3, 1, 3, 2, 1]) == [3, 1, 2]

    def test_chunk_splits_into_max_size_batches(self):
        assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_chunk_rejects_non_positive_size(self):
        with pytest.raises(ValueError):
            chunk([1, 2], 0)


def _route(method, url, body, headers):
    if "chat/completions" in url:
        return {"choices": [{"message": {"content": "a feline"}}]}
    if "images/generations" in url:
        return {"data": [{"b64_json": base64.b64encode(b"PNG").decode()}]}
    if "translate_tts" in url:
        return b"MP3"
    raise AssertionError(url)


def _hub():
    return ProviderHub(
        LLMSettings(provider="openai", openai=OpenAICompatibleLLMSettings(api_key="k")),
        # An Auto-detect map so a voiceless tts rule (English) resolves to google_translate.
        TTSSettings(
            provider="google_translate",
            auto_voices={"en": "google_translate:en"},
        ),
        http=FakeHttpClient(responder=_route),
    )


class TestGenerationService:
    def test_generate_text_interpolates_prompt(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="text", prompt="Define {{Word}}", target_field="Definition"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "text"
        assert result.text == "a feline"

    def test_generate_image_returns_bytes(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="image", prompt="a {{Word}}", target_field="Pic"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "image"
        assert result.data == b"PNG"
        assert result.ext == "png"

    def test_generate_tts_uses_prompt_text(self):
        service = GenerationService(_hub())
        # An explicit language skips detection; the Auto-detect map resolves the provider/voice.
        rule = SmartNotesFieldRule(
            kind="tts", prompt="say {{Word}}", target_field="Audio", language="en"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "tts"
        assert result.data == b"MP3"
        assert result.ext == "mp3"

    def test_generate_tts_falls_back_to_source_field(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="tts", source_field="Word", target_field="Audio", language="en"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "tts"
        assert result.data == b"MP3"


class TestMarkdownConversion:
    def test_bold_and_italic(self):
        assert convert_markdown_to_html("**b** and *i*") == (
            "<strong>b</strong> and <em>i</em>"
        )

    def test_underscores_render_as_emphasis(self):
        assert convert_markdown_to_html("__b__ _i_") == (
            "<strong>b</strong> <em>i</em>"
        )

    def test_headers_convert_by_level(self):
        assert convert_markdown_to_html("# Title\n") == "<h1>Title</h1><br>"
        assert convert_markdown_to_html("### Sub\n") == "<h3>Sub</h3><br>"

    def test_newlines_become_br(self):
        assert convert_markdown_to_html("a\nb") == "a<br>b"

    def test_leading_whitespace_becomes_nbsp(self):
        assert convert_markdown_to_html("  hi") == "&nbsp;&nbsp;hi"

    def test_plain_text_passes_through(self):
        assert convert_markdown_to_html("a feline") == "a feline"


def _rule(target, *, prompt="", source="", kind="text"):
    return SmartNotesFieldRule(
        target_field=target, prompt=prompt, source_field=source, kind=kind
    )


class TestOrderRules:
    def test_linear_chain_orders_dependency_first(self):
        a = _rule("A", source="Word")
        b = _rule("B", prompt="from {{A}}")
        c = _rule("C", prompt="from {{B}}")
        ordered = order_rules([c, b, a])
        targets = [r.target_field for r in ordered]
        assert targets.index("A") < targets.index("B") < targets.index("C")

    def test_diamond_orders_root_before_both_branches_before_join(self):
        root = _rule("Root", source="Word")
        left = _rule("Left", prompt="{{Root}}")
        right = _rule("Right", prompt="{{Root}}")
        join = _rule("Join", prompt="{{Left}} {{Right}}")
        ordered = [r.target_field for r in order_rules([join, left, right, root])]
        assert ordered.index("Root") < ordered.index("Left")
        assert ordered.index("Root") < ordered.index("Right")
        assert ordered.index("Left") < ordered.index("Join")
        assert ordered.index("Right") < ordered.index("Join")

    def test_independent_rules_keep_input_order(self):
        a = _rule("A", source="W")
        b = _rule("B", source="W")
        assert order_rules([a, b]) == [a, b]

    def test_cycle_raises(self):
        a = _rule("A", prompt="{{B}}")
        b = _rule("B", prompt="{{A}}")
        with pytest.raises(SmartNotesCycleError):
            order_rules([a, b])

    def test_self_reference_raises(self):
        with pytest.raises(SmartNotesCycleError):
            order_rules([_rule("A", prompt="{{A}}")])

    def test_field_matching_is_case_insensitive(self):
        producer = _rule("Def", source="Word")
        consumer = _rule("Usage", prompt="uses {{def}}")
        ordered = [r.target_field for r in order_rules([consumer, producer])]
        assert ordered.index("Def") < ordered.index("Usage")

    def test_tts_prompt_creates_a_dependency(self):
        producer = _rule("Reading", source="Word")
        tts = _rule("Audio", prompt="{{Reading}}", kind="tts")
        ordered = [r.target_field for r in order_rules([tts, producer])]
        assert ordered.index("Reading") < ordered.index("Audio")

    def test_explicit_only_dependency_orders(self):
        # No prompt ref, but an explicit depends_on edge must still order A before B.
        producer = _rule("A", prompt="static")
        consumer = SmartNotesFieldRule(
            target_field="B", prompt="static", depends_on=[FieldDep(field="A")]
        )
        ordered = [r.target_field for r in order_rules([consumer, producer])]
        assert ordered.index("A") < ordered.index("B")

    def test_union_of_derived_and_explicit_dependencies_orders(self):
        a = _rule("A", source="Word")
        note = _rule("Note", prompt="static")
        b = SmartNotesFieldRule(
            target_field="B",
            prompt="from {{A}}",
            depends_on=[FieldDep(field="Note", kind="soft")],
        )
        ordered = [r.target_field for r in order_rules([b, note, a])]
        assert ordered.index("A") < ordered.index("B")
        assert ordered.index("Note") < ordered.index("B")

    def test_cycle_via_explicit_dependency_raises(self):
        a = SmartNotesFieldRule(
            target_field="A", prompt="static", depends_on=[FieldDep(field="B")]
        )
        b = SmartNotesFieldRule(
            target_field="B", prompt="static", depends_on=[FieldDep(field="A")]
        )
        with pytest.raises(SmartNotesCycleError):
            order_rules([a, b])

    def test_all_soft_cycle_is_generatable(self):
        # Two fields that SOFTLY reference each other (Auto-prompt's POS <-> Definition): a soft
        # edge is optional metadata the ordering can break, so this must NOT raise — one field is
        # generated first (without the other's optional value), then the second uses it.
        a = SmartNotesFieldRule(
            target_field="A",
            prompt="uses {{B}}",
            depends_on=[FieldDep(field="B", kind="soft")],
        )
        b = SmartNotesFieldRule(
            target_field="B",
            prompt="uses {{A}}",
            depends_on=[FieldDep(field="A", kind="soft")],
        )
        ordered = [r.target_field for r in order_rules([a, b])]  # no raise
        assert set(ordered) == {"A", "B"}

    def test_soft_back_edge_dropped_hard_forward_kept(self):
        # A --hard--> B with a SOFT back-edge B --soft--> A: the hard edge orders A before B and
        # the soft back-edge is dropped (broken) so ordering still terminates.
        a = SmartNotesFieldRule(
            target_field="A",
            prompt="uses {{B}}",
            depends_on=[FieldDep(field="B", kind="soft")],
        )
        b = _rule("B", prompt="uses {{A}}")  # derived hard dep on A
        ordered = [r.target_field for r in order_rules([a, b])]
        assert ordered == ["A", "B"]  # hard A->B wins; soft B->A broken


class TestShouldSkipRule:
    def test_skips_when_all_sources_blank(self):
        rule = _rule("Def", prompt="define {{Word}}")
        rule.overwrite = True
        assert should_skip_rule(rule, {"Word": ""}, allow_empty_fields=False)

    def test_generates_when_allow_empty_fields(self):
        rule = _rule("Def", prompt="define {{Word}}")
        rule.overwrite = True
        assert not should_skip_rule(rule, {"Word": ""}, allow_empty_fields=True)

    def test_generates_when_any_source_has_a_value(self):
        rule = _rule("Def", prompt="{{A}} {{B}}")
        rule.overwrite = True
        assert not should_skip_rule(rule, {"A": "", "B": "x"}, allow_empty_fields=False)

    def test_skips_when_target_already_filled(self):
        rule = _rule("Def", prompt="define {{Word}}")
        fields = {"Word": "cat", "Def": "already here"}
        assert should_skip_rule(rule, fields, allow_empty_fields=False)

    def test_overwrites_when_rule_overwrite_set(self):
        rule = _rule("Def", prompt="define {{Word}}")
        rule.overwrite = True
        fields = {"Word": "cat", "Def": "already here"}
        assert not should_skip_rule(rule, fields, allow_empty_fields=False)

    def test_rule_with_no_source_refs_is_not_skipped_for_emptiness(self):
        rule = _rule("Def", prompt="a static prompt")
        rule.overwrite = True
        assert not should_skip_rule(rule, {}, allow_empty_fields=False)


class _RecordingLLM(FakeLLMProvider):
    """Fake LLM that echoes per-field text (the model is fixed at construction, not per call)."""

    def __init__(self, by_target=None):
        super().__init__()
        self._by_target = by_target or {}

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        return self._by_target.get(prompt, f"generated:{prompt}")

    def generate_image(self, prompt, *, size="1024x1024"):
        return b"IMG"


class _RecordingTTS(FakeTTSProvider):
    """Fake TTS that records the voice + text it is asked to synthesize."""

    def __init__(self):
        super().__init__()
        self.calls: list = []

    def synthesize(self, text, *, lang=None, voice=None):
        self.calls.append((text, voice))
        return b"AUDIO"


def _config(base, fields):
    """Build a SmartNotesNoteTypeConfig from (field, kwargs) tuples."""
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field=base,
        fields=[SmartNotesFieldConfig(field=name, **kw) for name, kw in fields],
    )


class TestGenerateNote:
    def test_chains_text_output_into_a_downstream_prompt(self):
        llm = _RecordingLLM(by_target={"define cat": "a feline"})
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Def", dict(enabled=True, type="text", prompt="define {{Word}}")),
                (
                    "Usage",
                    dict(enabled=True, type="text", prompt="use {{Def}} in a sentence"),
                ),
            ],
        )
        results, _blocked = service.generate_note(
            config, {"Word": "cat", "Def": "", "Usage": ""}
        )
        targets = [rule.target_field for rule, _ in results]
        assert targets == ["Def", "Usage"]
        # The downstream prompt saw the freshly generated value, not the blank field.
        usage_result = dict((r.target_field, res) for r, res in results)["Usage"]
        assert usage_result.text == "generated:use a feline in a sentence"

    def test_base_field_is_never_generated(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Word", dict(enabled=True, type="text", prompt="ignore")),  # base
                ("Def", dict(enabled=True, type="text", prompt="define {{Word}}")),
            ],
        )
        results, _blocked = service.generate_note(config, {"Word": "cat", "Def": ""})
        assert [r.target_field for r, _ in results] == ["Def"]

    def test_skips_already_filled_target_without_overwrite(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word", [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))]
        )
        results, _blocked = service.generate_note(
            config, {"Word": "cat", "Def": "filled"}
        )
        assert results == []

    def test_force_overwrite_regenerates_filled_target(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word", [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))]
        )
        results, _blocked = service.generate_note(
            config, {"Word": "cat", "Def": "filled"}, force_overwrite=True
        )
        assert [r.target_field for r, _ in results] == ["Def"]

    def test_per_field_overwrite_regenerates_filled_target(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                (
                    "Def",
                    dict(
                        enabled=True,
                        type="text",
                        prompt="define {{Word}}",
                        overwrite=True,
                    ),
                )
            ],
        )
        results, _blocked = service.generate_note(
            config, {"Word": "cat", "Def": "filled"}
        )
        assert [r.target_field for r, _ in results] == ["Def"]

    def test_skips_field_with_all_blank_sources(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word", [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))]
        )
        results, _blocked = service.generate_note(config, {"Word": "", "Def": ""})
        assert results == []

    def test_disabled_field_is_skipped(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [("Def", dict(enabled=False, type="text", prompt="define {{Word}}"))],
        )
        results, _blocked = service.generate_note(config, {"Word": "cat", "Def": ""})
        assert results == []

    def test_cycle_raises(self):
        service = GenerationService(_stub_hub(llm=_RecordingLLM()))
        config = _config(
            "Word",
            [
                ("A", dict(enabled=True, type="text", prompt="{{B}}", overwrite=True)),
                ("B", dict(enabled=True, type="text", prompt="{{A}}", overwrite=True)),
            ],
        )
        with pytest.raises(SmartNotesCycleError):
            service.generate_note(config, {"A": "", "B": ""})


class TestGenerateNoteBlocking:
    def test_hard_prereq_empty_blocks_and_is_transitive(self):
        # Word is blank → Def (hard prereq Word) is blocked, which leaves Def blank, so Usage
        # (hard prereq Def) is blocked transitively. allow_empty_fields lets us isolate the
        # block gate from the all-blank-sources skip.
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Def", dict(enabled=True, type="text", prompt="define {{Word}}")),
                ("Usage", dict(enabled=True, type="text", prompt="use {{Def}}")),
            ],
        )
        results, blocked = service.generate_note(
            config, {"Word": "", "Def": "", "Usage": ""}
        )
        assert results == []
        targets = {b.target_field for b in blocked}
        assert targets == {"Def", "Usage"}
        def_block = next(b for b in blocked if b.target_field == "Def")
        assert def_block.missing == ["Word"]

    def test_soft_prereq_empty_still_generates(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Note", dict(enabled=True, type="text", prompt="static")),
                (
                    "Def",
                    dict(
                        enabled=True,
                        type="text",
                        prompt="define {{Word}}",
                        depends_on=[FieldDep(field="Note", kind="soft")],
                    ),
                ),
            ],
        )
        # Note is blank, but it is only a SOFT prereq of Def, so Def still generates.
        results, blocked = service.generate_note(
            config, {"Word": "cat", "Note": "", "Def": ""}
        )
        assert "Def" in [r.target_field for r, _ in results]
        assert all(b.target_field != "Def" for b in blocked)

    def test_hard_prereq_present_generates(self):
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))],
        )
        results, blocked = service.generate_note(config, {"Word": "cat", "Def": ""})
        assert [r.target_field for r, _ in results] == ["Def"]
        assert blocked == []

    def test_image_hard_prereq_does_not_block_when_it_generates(self):
        # Pic is an IMAGE field (its embed ref is NOT chained into the working map). Caption
        # hard-depends on Pic via {{Pic}}; because Pic generated successfully this run it counts
        # as produced, so Caption is NOT spuriously blocked.
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Pic", dict(enabled=True, type="image", prompt="draw {{Word}}")),
                (
                    "Caption",
                    dict(enabled=True, type="text", prompt="caption for {{Pic}}"),
                ),
            ],
        )
        # allow_empty_fields isolates the BLOCK gate from the pre-existing empty-source skip
        # (an image embed ref is never chained into ``working``, so {{Pic}} reads blank there).
        results, blocked = service.generate_note(
            config, {"Word": "cat", "Pic": "", "Caption": ""}, allow_empty_fields=True
        )
        targets = [r.target_field for r, _ in results]
        assert targets == ["Pic", "Caption"]
        assert blocked == []

    def test_explicit_hard_dep_on_non_ref_field_blocks_when_empty(self):
        # Def's prompt does not reference Note, but an explicit HARD depends_on makes Note a
        # blocking prerequisite. Note is blank → Def is blocked at the generate_note gate.
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Note", dict(enabled=False, type="text")),
                (
                    "Def",
                    dict(
                        enabled=True,
                        type="text",
                        prompt="define {{Word}}",
                        depends_on=[FieldDep(field="Note", kind="hard")],
                    ),
                ),
            ],
        )
        results, blocked = service.generate_note(
            config, {"Word": "cat", "Note": "", "Def": ""}
        )
        assert results == []
        assert [b.target_field for b in blocked] == ["Def"]
        assert blocked[0].missing == ["Note"]

    def test_self_referential_explicit_dep_raises(self):
        # An explicit depends_on naming the field itself is a self-loop the engine must reject
        # (the model layer permits it; the engine's ordering does not).
        service = GenerationService(_stub_hub(llm=_RecordingLLM()))
        config = _config(
            "Word",
            [
                (
                    "Def",
                    dict(
                        enabled=True,
                        type="text",
                        prompt="define {{Word}}",
                        depends_on=[FieldDep(field="Def", kind="hard")],
                    ),
                )
            ],
        )
        with pytest.raises(SmartNotesCycleError):
            service.generate_note(config, {"Word": "cat", "Def": ""})

    def test_explicit_dep_on_base_field_blocks_when_base_empty(self):
        # An explicit hard dep on the base field is honoured at the blocking gate: base blank
        # → the dependent is blocked.
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                (
                    "Def",
                    dict(
                        enabled=True,
                        type="text",
                        prompt="static",
                        depends_on=[FieldDep(field="Word", kind="hard")],
                    ),
                )
            ],
        )
        results, blocked = service.generate_note(config, {"Word": "", "Def": ""})
        assert results == []
        assert [b.target_field for b in blocked] == ["Def"]
        assert blocked[0].missing == ["Word"]

    def test_already_filled_prereq_counts_present(self):
        # Def is already filled and not overwritten → it is "present" (non-empty) for Usage's
        # hard prereq, so Usage is NOT blocked (it generates). Def itself is skipped (filled).
        llm = _RecordingLLM()
        service = GenerationService(_stub_hub(llm=llm))
        config = _config(
            "Word",
            [
                ("Def", dict(enabled=True, type="text", prompt="define {{Word}}")),
                ("Usage", dict(enabled=True, type="text", prompt="use {{Def}}")),
            ],
        )
        results, blocked = service.generate_note(
            config, {"Word": "cat", "Def": "already here", "Usage": ""}
        )
        assert [r.target_field for r, _ in results] == ["Usage"]
        assert blocked == []


class TestPerFieldOverrides:
    def test_text_rule_model_override_selects_a_provider_instance(self):
        hub = _stub_hub(llm=_RecordingLLM())
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text",
            prompt="hi {{Word}}",
            target_field="Def",
            model="rule-model",
            provider="gemini",
        )
        service.generate(rule, {"Word": "x"})
        # The model is fixed at construction: the service asks the hub for a provider INSTANCE
        # configured with that (provider, model), never threading model into generate_text.
        # A text rule pins the text model; image_model is left untouched.
        assert hub.llm_overrides == [("gemini", "rule-model", "")]

    def test_text_rule_without_override_uses_the_active_provider(self):
        hub = _stub_hub(llm=_RecordingLLM())
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text", prompt="hi {{Word}}", target_field="Def"
        )
        service.generate(rule, {"Word": "x"})
        # An empty override means "use the configured active provider".
        assert hub.llm_overrides == [("", "", "")]

    def test_image_rule_model_override_selects_a_provider_instance(self):
        hub = _stub_hub(llm=_RecordingLLM())
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="image", prompt="a {{Word}}", target_field="Pic", model="img-model"
        )
        service.generate(rule, {"Word": "cat"})
        # An image rule pins the IMAGE model (so generate_image targets it), not the text model.
        assert hub.llm_overrides == [("", "", "img-model")]

    def test_tts_rule_voice_override_speaks_only_referenced_field(self):
        # A TTS field voices only its {{refs}}' content, never the prompt's prose — so
        # "hello {{Word}}" speaks just the Word value ("cat"), not the literal "hello".
        tts = _RecordingTTS()
        service = GenerationService(_stub_hub(tts=tts))
        rule = SmartNotesFieldRule(
            kind="tts", prompt="hello {{Word}}", target_field="Audio", voice="en-US-X"
        )
        service.generate(rule, {"Word": "cat"})
        assert tts.calls == [("cat", "en-US-X")]

    def test_tts_concrete_voice_builds_the_rules_provider(self):
        tts = _RecordingTTS()
        hub = _stub_hub(tts=tts)
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="tts",
            prompt="hi",
            target_field="Audio",
            voice="vi-VN-HoaiMyNeural",
            provider="edge_tts",
        )
        service.generate(rule, {})
        # The pinned voice synthesizes on the rule's provider directly (no resolution).
        assert hub.tts_providers == ["edge_tts"]
        assert tts.calls == [("hi", "vi-VN-HoaiMyNeural")]

    def test_tts_auto_detect_resolves_provider_and_voice_from_the_map(self):
        tts = _RecordingTTS()
        # language="ja" skips detection; the map resolves (provider, voice).
        hub = _stub_hub(tts=tts, auto_voices={"ja": ("edge_tts", "ja-JP-NanamiNeural")})
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="tts", prompt="こんにちは", target_field="Audio", language="ja"
        )
        service.generate(rule, {})
        assert hub.tts_providers == ["edge_tts"]
        assert tts.calls == [("こんにちは", "ja-JP-NanamiNeural")]

    def test_tts_auto_detect_unmapped_language_raises(self):
        hub = _stub_hub(tts=_RecordingTTS(), auto_voices={})
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="tts", prompt="x", target_field="Audio", language="ja"
        )
        with pytest.raises(ProviderError):
            service.generate(rule, {})

    def test_tts_auto_detect_runs_detection_then_raises_when_unmapped(self):
        # No explicit language → the engine DETECTS; with a None LLM the detector yields no
        # language and the empty map has no fallback, so generation fails with a clear
        # ProviderError instead of silently using a wrong-language voice.
        hub = _stub_hub(llm=None, tts=_RecordingTTS(), auto_voices={})
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(kind="tts", prompt="x", target_field="Audio")
        with pytest.raises(ProviderError):
            service.generate(rule, {})

    def test_text_result_is_markdown_converted(self):
        llm = _RecordingLLM(by_target={"go": "**bold**"})
        service = GenerationService(_stub_hub(llm=llm))
        rule = SmartNotesFieldRule(kind="text", prompt="go", target_field="Def")
        result = service.generate(rule, {})
        assert result.text == "<strong>bold</strong>"


# ---------------------------------------------------------------------------
# Auto-smart: prompt-build (pure), result-apply (pure), and the thin glue.
# ---------------------------------------------------------------------------


class TestAutoSmartPromptBuild:
    def test_prompt_carries_persona_base_field_and_targets(self):
        prompt = build_auto_smart_prompt("Basic", "Word", ["Meaning", "Audio"])
        assert "senior language master" in prompt
        assert "{{Word}}" in prompt
        assert "Meaning" in prompt and "Audio" in prompt
        # It must demand JSON output so the reply is parseable.
        assert "JSON" in prompt

    def test_prompt_asks_for_depends_on(self):
        prompt = build_auto_smart_prompt("Basic", "Word", ["Meaning"])
        assert "depends_on" in prompt
        assert "hard" in prompt and "soft" in prompt
        # No existing edges → no KEEP-these block is emitted.
        assert "ALREADY" not in prompt

    def test_prompt_serializes_an_existing_edge_when_graph_non_empty(self):
        prompt = build_auto_smart_prompt(
            "Basic",
            "Word",
            ["Meaning", "Audio"],
            existing_deps={"Audio": [AutoSmartDep(field="Meaning", kind="soft")]},
        )
        # The existing edge is spelled out so the model keeps it instead of clobbering it.
        assert "KEEP" in prompt
        assert '"Audio" depends on "Meaning" (soft)' in prompt


class TestAutoSmartParse:
    def test_parses_a_clean_json_object(self):
        raw = '{"Meaning": {"type": "text", "prompt": "Define {{Word}}"}}'
        out = parse_auto_smart_response(raw)
        assert out == {"Meaning": AutoSmartField(type="text", prompt="Define {{Word}}")}

    def test_tolerates_code_fences_and_prose(self):
        raw = (
            "Sure! Here you go:\n```json\n"
            '{"Audio": {"type": "tts", "prompt": "{{Word}}"}}\n```\nHope that helps.'
        )
        out = parse_auto_smart_response(raw)
        assert out["Audio"] == AutoSmartField(type="tts", prompt="{{Word}}")

    def test_invalid_type_falls_back_to_text(self):
        out = parse_auto_smart_response('{"X": {"type": "video", "prompt": "p"}}')
        assert out["X"].type == "text"

    def test_missing_prompt_defaults_to_empty(self):
        out = parse_auto_smart_response('{"X": {"type": "text"}}')
        assert out["X"].prompt == ""

    def test_non_object_value_is_ignored(self):
        out = parse_auto_smart_response('{"X": "nope", "Y": {"type": "text"}}')
        assert "X" not in out and "Y" in out

    def test_no_json_raises(self):
        with pytest.raises(ProviderError):
            parse_auto_smart_response("I cannot help with that.")

    def test_malformed_json_raises(self):
        with pytest.raises(ProviderError):
            parse_auto_smart_response("{not valid json}")

    def test_reads_depends_on_with_kind_default_hard(self):
        raw = (
            '{"Example": {"type": "text", "prompt": "p", '
            '"depends_on": [{"field": "Meaning"}, {"field": "IPA", "kind": "soft"}]}}'
        )
        out = parse_auto_smart_response(raw)
        assert out["Example"].depends_on == (
            AutoSmartDep(field="Meaning", kind="hard"),
            AutoSmartDep(field="IPA", kind="soft"),
        )

    def test_unknown_kind_becomes_hard(self):
        raw = '{"X": {"type": "text", "depends_on": [{"field": "Y", "kind": "weird"}]}}'
        out = parse_auto_smart_response(raw)
        assert out["X"].depends_on == (AutoSmartDep(field="Y", kind="hard"),)

    def test_entry_without_field_is_dropped(self):
        raw = (
            '{"X": {"type": "text", "depends_on": [{"kind": "hard"}, {"field": "Y"}]}}'
        )
        out = parse_auto_smart_response(raw)
        assert out["X"].depends_on == (AutoSmartDep(field="Y", kind="hard"),)

    def test_self_reference_is_ignored(self):
        raw = '{"X": {"type": "text", "depends_on": [{"field": "x"}, {"field": "Y"}]}}'
        out = parse_auto_smart_response(raw)
        assert out["X"].depends_on == (AutoSmartDep(field="Y", kind="hard"),)

    def test_absent_depends_on_is_empty_tuple(self):
        out = parse_auto_smart_response('{"X": {"type": "text", "prompt": "p"}}')
        assert out["X"].depends_on == ()


class TestAutoSmartApply:
    def _config(self):
        return SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(field="Meaning", enabled=True),
                SmartNotesFieldConfig(
                    field="Locked", enabled=True, prompt_locked=True, prompt="keep me"
                ),
                SmartNotesFieldConfig(field="Off", enabled=False),
            ],
        )

    def test_only_enabled_unlocked_fields_are_overwritten(self):
        config = self._config()
        suggestions = {
            "Meaning": AutoSmartField(type="text", prompt="Define {{Word}}"),
            "Locked": AutoSmartField(type="image", prompt="overwrite attempt"),
            "Off": AutoSmartField(type="tts", prompt="overwrite attempt"),
        }
        updated = apply_auto_smart(config, suggestions)
        by_name = {f.field: f for f in updated.fields}
        assert by_name["Meaning"].prompt == "Define {{Word}}"
        assert by_name["Meaning"].type == "text"
        # Locked + disabled fields are untouched.
        assert by_name["Locked"].prompt == "keep me"
        assert by_name["Off"].prompt == ""

    def test_input_config_is_not_mutated(self):
        config = self._config()
        apply_auto_smart(config, {"Meaning": AutoSmartField(type="text", prompt="new")})
        assert config.fields[0].prompt == ""  # original untouched

    def test_field_without_suggestion_is_untouched(self):
        config = self._config()
        updated = apply_auto_smart(config, {})
        assert updated.fields[0].prompt == ""

    def test_candidate_fields_excludes_base_disabled_and_locked(self):
        config = self._config()
        assert candidate_fields(config) == ["Meaning"]

    def test_depends_on_fills_a_gap_field_and_maps_to_field_dep(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[SmartNotesFieldConfig(field="Example", enabled=True)],
        )
        suggestions = {
            "Example": AutoSmartField(
                type="text",
                prompt="Use {{Meaning}} in a sentence",  # the prompt references the dep
                depends_on=(AutoSmartDep(field="Meaning", kind="soft"),),
            )
        }
        updated = apply_auto_smart(config, suggestions)
        # machine-suggested → auto=True (so reconcile self-cleans it if a later prompt drops the ref)
        assert updated.fields[0].depends_on == [
            FieldDep(field="Meaning", kind="soft", auto=True)
        ]

    def test_suggested_dep_not_in_prompt_is_dropped(self):
        # A suggested dep the generated prompt does NOT reference is a dead edge (it would order/
        # block but pass no value), so apply_auto_smart drops it — every edge maps to a {{ref}}.
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[SmartNotesFieldConfig(field="Example", enabled=True)],
        )
        suggestions = {
            "Example": AutoSmartField(
                type="text",
                prompt="Write a sentence with {{Word}}",  # references Word, NOT Meaning
                depends_on=(
                    AutoSmartDep(field="Word", kind="hard"),
                    AutoSmartDep(field="Meaning", kind="soft"),  # not in the prompt → dropped
                ),
            )
        }
        updated = apply_auto_smart(config, suggestions)
        assert updated.fields[0].depends_on == [
            FieldDep(field="Word", kind="hard", auto=True)
        ]

    def test_existing_depends_on_is_preserved_not_clobbered(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Example",
                    enabled=True,
                    depends_on=[FieldDep(field="Word", kind="hard")],
                )
            ],
        )
        suggestions = {
            "Example": AutoSmartField(
                type="text",
                prompt="p",
                depends_on=(AutoSmartDep(field="Meaning", kind="soft"),),
            )
        }
        updated = apply_auto_smart(config, suggestions)
        # The user-drawn edge stays; the suggestion does not overwrite it.
        assert updated.fields[0].depends_on == [FieldDep(field="Word", kind="hard")]

    def test_depends_on_skips_locked_and_disabled_fields(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(field="Locked", enabled=True, prompt_locked=True),
                SmartNotesFieldConfig(field="Off", enabled=False),
            ],
        )
        dep = (AutoSmartDep(field="Word", kind="hard"),)
        suggestions = {
            "Locked": AutoSmartField(type="text", prompt="p", depends_on=dep),
            "Off": AutoSmartField(type="text", prompt="p", depends_on=dep),
        }
        updated = apply_auto_smart(config, suggestions)
        by_name = {f.field: f for f in updated.fields}
        assert by_name["Locked"].depends_on == []
        assert by_name["Off"].depends_on == []


class _CannedLLM(FakeLLMProvider):
    def __init__(self, text):
        super().__init__()
        self._fixed_text = text

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        return self._fixed_text


class _RaisingLLM(FakeLLMProvider):
    """A fake LLM whose ``generate_text`` raises ``exc`` (to prove it is/ isn't called)."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        raise self._exc


class TestAutoSmartGenerate:
    def test_calls_llm_and_applies_the_result(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[SmartNotesFieldConfig(field="Meaning", enabled=True)],
        )
        author = PromptAuthor(
            _CannedLLM('{"Meaning": {"type": "text", "prompt": "Define {{Word}}"}}')
        )
        updated = author.auto_smart(config)
        assert updated.fields[0].prompt == "Define {{Word}}"

    def test_no_candidates_is_a_noop(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(field="Locked", enabled=True, prompt_locked=True)
            ],
        )
        # No candidates → the LLM is never called (its generate_text would assert otherwise).
        author = PromptAuthor(_RaisingLLM(AssertionError("must not call the LLM")))
        assert author.auto_smart(config) == config

    def test_provider_failure_propagates(self):
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[SmartNotesFieldConfig(field="Meaning", enabled=True)],
        )
        author = PromptAuthor(_RaisingLLM(ProviderError("boom")))
        with pytest.raises(ProviderError):
            author.auto_smart(config)


class TestSmartNotesPlugin:
    _HOOKS = (
        "browser_will_show_context_menu",
        "browser_sidebar_will_show_context_menu",
        "editor_did_init_buttons",
        "editor_will_show_context_menu",
        "reviewer_did_show_question",
    )

    def test_enable_subscribes_all_hooks_disable_removes_them(self, gui_hooks):
        import types

        from omnia.plugins.smart_notes import SmartNotesPlugin
        from omnia.plugins.smart_notes.config import SmartNotesSettings

        ctx = types.SimpleNamespace(settings=SmartNotesSettings(), providers=_hub())
        plugin = SmartNotesPlugin()
        plugin.on_enable(ctx)
        assert all(getattr(gui_hooks, name).count() == 1 for name in self._HOOKS)
        plugin.on_disable(ctx)
        assert all(getattr(gui_hooks, name).count() == 0 for name in self._HOOKS)


class _FakeNote:
    """A dict-backed stand-in for an Anki note (keys/__getitem__/__setitem__ + id)."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)
        self.id = 1

    def keys(self):
        return list(self._values.keys())

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def note_type(self):
        return {"name": "Basic"}


class _FakeEditor:
    def __init__(self, note: _FakeNote) -> None:
        self.note = note
        self.web = None  # set_button_enabled is a safe no-op
        self.reloaded = False

    def loadNote(self) -> None:  # Anki's API name (camelCase)
        self.reloaded = True


class TestEditorGeneratePath:
    """The editor ✨ "Generate" path must unpack ``generate_note``'s (results, blocked) tuple."""

    def test_generate_into_note_unpacks_tuple_and_writes_field(self, monkeypatch):
        import types

        from omnia.core import anki_compat
        from omnia.plugins.smart_notes import SmartNotesPlugin

        config = _config(
            "Word",
            [("Def", dict(enabled=True, type="text", prompt="define {{Word}}"))],
        )
        settings = SmartNotesSettings(note_types=[config])
        llm = _RecordingLLM(by_target={"define cat": "a feline"})

        plugin = SmartNotesPlugin()
        plugin._service = GenerationService(_stub_hub(llm=llm))
        plugin._settings = lambda: settings

        captured: dict = {}
        monkeypatch.setattr(
            anki_compat, "update_note", lambda note: captured.setdefault("note", note)
        )
        # aqt.utils.tooltip is imported lazily inside _apply_to_editor.
        monkeypatch.setitem(
            __import__("sys").modules,
            "aqt.utils",
            types.SimpleNamespace(
                tooltip=lambda *_a, **_k: None, showWarning=lambda *_a, **_k: None
            ),
        )

        note = _FakeNote({"Word": "cat", "Def": ""})
        editor = _FakeEditor(note)
        # Drives the synchronous QueryOp stub: op() must return a list, not the (list, list) tuple.
        plugin._generate_into_note(editor, note, config)

        assert note["Def"] == "a feline"
        assert editor.reloaded is True
        assert captured.get("note") is note


class TestBatchSummary:
    def test_message_reports_each_count(self):
        from omnia.plugins.smart_notes.integration.batch import BatchSummary

        summary = BatchSummary(processed=3, failed=1, skipped=2)
        assert summary.message() == "Processed 3 note(s), 1 failed, 2 skipped."

    def test_message_omits_zero_counts(self):
        from omnia.plugins.smart_notes.integration.batch import BatchSummary

        assert BatchSummary(processed=2).message() == "Processed 2 note(s)."

    def test_cancelled_message_is_prefixed(self):
        from omnia.plugins.smart_notes.integration.batch import BatchSummary

        summary = BatchSummary(processed=1, cancelled=True)
        assert summary.message().startswith("Cancelled — ")

    def test_blocked_count_is_surfaced(self):
        from omnia.plugins.smart_notes.integration.batch import BatchSummary

        summary = BatchSummary(processed=2, blocked=3)
        assert summary.message() == (
            "Processed 2 note(s), 3 blocked — missing prerequisites."
        )


class TestSmartNotesSettingsDefaults:
    def test_generate_at_review_defaults_off(self):
        assert SmartNotesSettings().generate_at_review is False

    def test_generate_at_review_round_trips(self):
        settings = SmartNotesSettings(generate_at_review=True)
        rebuilt = SmartNotesSettings(**settings.dict())
        assert rebuilt.generate_at_review is True


# ---------------------------------------------------------------------------
# GenerationService contract — the SAME functional assertions, fake + real backends.
#
# The provider sweep (``tests/providers/test_llm.py`` / ``test_tts.py``) proves each provider
# works in isolation; these prove the *feature that uses them* works against its providers. The
# context is a ProviderHub-shaped ``hub`` (exposing ``.llm()`` / ``.tts()``) supplied by a
# fixture each concrete subclass overrides. Because LLM-gen and TTS-gen parametrize over
# different provider sets, there are TWO bases:
#
# * :class:`_SmartNotesLLMGenContract`  → text + (capability-gated) image rules.
# * :class:`_SmartNotesTTSGenContract`  → tts rules.
#
# Each base has a Fake subclass (canned providers — always runs, free) and a Real subclass
# whose ``hub`` fixture is PARAMETRIZED over every provider. Policy mirrors the provider sweep:
# skip without creds, ``xfail`` on quota/token/transient.
# ---------------------------------------------------------------------------


class _StubHub:
    """A minimal ProviderHub-shaped stub exposing ``llm(...)`` / ``tts(provider=)`` / resolve.

    Records the per-rule ``(provider, model, image_model)`` LLM overrides and the ``tts``
    provider asked for. ``auto_voices`` maps a language code to ``(provider, voice)`` for the
    Auto-detect path; ``resolve_auto_voice`` raises a ProviderError for an unmapped language
    (mirroring the real hub), so the engine's Auto-detect contract is exercised offline.
    """

    def __init__(self, *, llm=None, tts=None, auto_voices=None):
        self._llm = llm
        self._tts = tts
        self.llm_overrides: list = []
        self.tts_providers: list = []
        self._auto_voices = auto_voices or {}

    def llm(self, *, model: str = "", image_model: str = "", provider: str = ""):
        self.llm_overrides.append((provider, model, image_model))
        return self._llm

    def tts(self, *, provider: str = ""):
        self.tts_providers.append(provider)
        return self._tts

    def resolve_auto_voice(self, lang: str):
        if lang not in self._auto_voices:
            raise ProviderError(f"No Auto-detect voice set for language {lang!r}")
        return self._auto_voices[lang]


def _stub_hub(*, llm=None, tts=None, auto_voices=None):
    """A minimal ProviderHub-shaped stub exposing ``llm(...)`` / ``tts(provider=)`` / resolve."""
    return _StubHub(llm=llm, tts=tts, auto_voices=auto_voices)


class _SmartNotesLLMGenContract:
    """Shared assertions for LLM-backed smart_notes rules; subclasses supply ``hub``."""

    @pytest.fixture
    def hub(self):
        raise NotImplementedError

    def test_text_rule_generates(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text",
            prompt="Define {{Word}} in one short sentence.",
            target_field="Def",
        )
        result = call_or_xfail(service.generate, rule, {"Word": "cat"})
        assert result.kind == "text"
        assert isinstance(result.text, str) and result.text.strip()

    def test_image_rule_generates_if_supported(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="image", prompt="a single red apple", target_field="Pic"
        )
        try:
            result = service.generate(rule, {})
        except ProviderError as exc:
            if is_provider_limit_error(exc):
                pytest.xfail(f"image-gen limit: {str(exc)[:160]}")
            pytest.skip(f"image gen unavailable: {str(exc)[:120]}")
        assert result.kind == "image"
        assert isinstance(result.data, (bytes, bytearray)) and result.data


class _SmartNotesTTSGenContract:
    """Shared assertions for TTS-backed smart_notes rules; subclasses supply ``hub``."""

    @pytest.fixture
    def hub(self):
        raise NotImplementedError

    def test_tts_rule_synthesizes(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        result = call_or_xfail(service.generate, rule, {"Word": "hello world"})
        assert result.kind == "tts"
        assert isinstance(result.data, (bytes, bytearray)) and result.data
        assert result.ext  # provider declared an audio extension


class TestSmartNotesLLMGenFake(_SmartNotesLLMGenContract):
    """LLM-backed rules against canned providers — always runs, no quota."""

    @pytest.fixture
    def hub(self):
        return _stub_hub(llm=FakeLLMProvider())


@pytest.mark.llm
class TestSmartNotesLLMGenReal(_SmartNotesLLMGenContract):
    """LLM-backed rules against EACH real provider (skips per provider without creds)."""

    @pytest.fixture(params=llm_provider_params())
    def hub(self, request):
        return _stub_hub(llm=real_llm_provider_for_or_skip(request.param))

    def test_generated_definition_matches_the_word(self, hub):
        # Beyond non-empty: the generated definition must actually be ABOUT the word.
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text",
            prompt="Define the word '{{Word}}' in one short sentence.",
            target_field="Def",
        )
        result = call_or_xfail(service.generate, rule, {"Word": "cat"})
        text = result.text.lower()
        assert any(
            kw in text for kw in ("animal", "feline", "mammal", "pet", "cat")
        ), f"definition of 'cat' had no relevant content: {result.text!r}"


class TestSmartNotesTTSGenFake(_SmartNotesTTSGenContract):
    """TTS-backed rules against a canned provider — always runs, no quota."""

    @pytest.fixture
    def hub(self):
        # The contract rule is voiceless (Auto-detect); with a None LLM the detector yields no
        # language (""), which the map resolves to the canned provider (empty voice).
        return _stub_hub(tts=FakeTTSProvider(), auto_voices={"": ("fake", "")})


class TestSmartNotesTTSGenReal(_SmartNotesTTSGenContract):
    """TTS-backed rules against EACH real provider (per-provider marks; skips when unavailable)."""

    @pytest.fixture(params=tts_provider_params())
    def hub(self, request):
        provider = real_tts_provider_for_or_skip(request.param)
        # Auto-detect with no LLM → lang "" → resolve to this provider (empty voice → the
        # provider's configured/default voice). Exercises the resolver + tts(provider=) path.
        return _stub_hub(tts=provider, auto_voices={"": (request.param, "")})

    def test_generated_audio_is_valid(self, hub):
        # Beyond non-empty: the field gets REAL audio in the provider's declared format.
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(kind="tts", prompt="{{Word}}", target_field="Audio")
        result = call_or_xfail(
            service.generate, rule, {"Word": "Hello, this is a real speech test."}
        )
        assert_valid_audio(result.data, result.ext)
