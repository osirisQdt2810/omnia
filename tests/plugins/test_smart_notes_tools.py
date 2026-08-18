"""Tests for the smart-notes tool seam: registry, pipeline, the ``ai`` tool, and compilation.

The pipeline matrix is the important part: it pins every attempt status and the fall-through
rule for each (declined / empty / broke / unknown / wrong kind), because that is the contract
the deterministic tools of later phases lean on. Pure logic — no Anki, no network.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest
from pydantic import BaseModel

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.config import (
    CompiledToolSpec,
    FieldDep,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
    default_tool_chain,
)
from omnia.plugins.smart_notes.engine import (
    GenerationService,
    compile_field_rule,
    order_rules,
)
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.rules import rule_prerequisites
from omnia.plugins.smart_notes.engine.tools import (
    TOOL_REGISTRY,
    AiTool,
    Empty,
    GenerationPipeline,
    NotApplicable,
    Produced,
    Tool,
    ToolChainError,
    ToolContext,
    ToolError,
    get_tool,
    register_tool,
    registered_tools,
    resolve_tool,
    tool_referenced_fields,
    tool_required_params,
    tools_catalog,
    unregister_tool,
)
from omnia.plugins.smart_notes.engine.tools.pipeline import ToolAttempt

# Every fake tool appends its name here when it runs, so a test can prove a later tool was
# never reached (or that a declining tool really did execute).
_RUNS: list[str] = []


class _ProduceTool(Tool):
    name: ClassVar[str] = "t_produce"
    label: ClassVar[str] = "Produce"
    description: ClassVar[str] = "Always produces."
    kinds: ClassVar[frozenset[str]] = frozenset({"text"})
    deterministic: ClassVar[bool] = True

    def run(self, request, ctx):
        _RUNS.append(self.name)
        return Produced(GenerationResult("text", text="made"))


class _DeclineTool(_ProduceTool):
    name: ClassVar[str] = "t_decline"
    label: ClassVar[str] = "Decline"

    def run(self, request, ctx):
        _RUNS.append(self.name)
        return NotApplicable("no match")


class _EmptyTool(_ProduceTool):
    name: ClassVar[str] = "t_empty"
    label: ClassVar[str] = "Empty"

    def run(self, request, ctx):
        _RUNS.append(self.name)
        return Empty("blank")


class _BoomTool(_ProduceTool):
    name: ClassVar[str] = "t_boom"
    label: ClassVar[str] = "Boom"

    def run(self, request, ctx):
        _RUNS.append(self.name)
        raise ToolError("kaput")


class _TerminalTool(_ProduceTool):
    """A tool whose OWN contract is "never decline, always raise" (see ``cloze_audio``)."""

    name: ClassVar[str] = "t_terminal"
    label: ClassVar[str] = "Terminal"

    def run(self, request, ctx):
        _RUNS.append(self.name)
        raise ToolError("would leak the answer")


class _RequiringTool(_ProduceTool):
    """A tool that names params the picker must not leave blank."""

    name: ClassVar[str] = "t_requires"
    label: ClassVar[str] = "Requires"
    uses_provider: ClassVar[bool] = False
    required_params: ClassVar[frozenset[str]] = frozenset({"sentence_field"})


class _ImageOnlyTool(_ProduceTool):
    name: ClassVar[str] = "t_image_only"
    label: ClassVar[str] = "Image only"
    kinds: ClassVar[frozenset[str]] = frozenset({"image"})


class _JunkTool(_ProduceTool):
    name: ClassVar[str] = "t_junk"
    label: ClassVar[str] = "Junk"

    def run(self, request, ctx):
        _RUNS.append(self.name)
        return None  # outside the outcome union


class _EchoParams(BaseModel):
    source_field: str
    suffix: str = "!"


class _ParamTool(_ProduceTool):
    name: ClassVar[str] = "t_param"
    label: ClassVar[str] = "Param"
    description: ClassVar[str] = "Echoes a field named by its params."
    params_model: ClassVar[type[BaseModel]] = _EchoParams

    @classmethod
    def referenced_fields(cls, params):
        source = str(params.get("source_field", "")).strip()
        return [source] if source else []

    def run(self, request, ctx):
        _RUNS.append(self.name)
        value = request.fields.get(request.params["source_field"], "")
        return Produced(GenerationResult("text", text=value + request.params["suffix"]))


class _UnavailableTool(_ProduceTool):
    name: ClassVar[str] = "t_unavailable"
    label: ClassVar[str] = "Unavailable"
    description: ClassVar[str] = "Never usable here."

    @classmethod
    def availability(cls, ctx):
        return "needs a WAV TTS provider"


class _ExplodingInitTool(_ProduceTool):
    """A tool that breaks before it can even run — its CONSTRUCTOR raises."""

    name: ClassVar[str] = "t_bad_init"
    label: ClassVar[str] = "Bad init"

    def __init__(self) -> None:
        raise ToolError("cannot start")


class _NoKindsTool(Tool):
    """A malformed tool class: it never declares the ``kinds`` ClassVar the gate reads."""

    name: ClassVar[str] = "t_no_kinds"
    label: ClassVar[str] = "No kinds"
    description: ClassVar[str] = "Malformed on purpose."
    deterministic: ClassVar[bool] = True

    def run(self, request, ctx):  # pragma: no cover - the kind gate breaks first
        raise AssertionError("a tool with no kinds must never be run")


# The two malformed tools above are deliberately NOT registered by the fixture: they would
# break every consumer that walks the whole registry (the catalog reads ``kinds``). The tests
# that need them register them individually, and the fixture's teardown still restores.
_FAKE_TOOLS: tuple[type[Tool], ...] = (
    _ProduceTool,
    _DeclineTool,
    _EmptyTool,
    _BoomTool,
    _TerminalTool,
    _RequiringTool,
    _ImageOnlyTool,
    _JunkTool,
    _ParamTool,
    _UnavailableTool,
)


@pytest.fixture
def fake_tools():
    """Register the fake tools for one test, then restore the real registry."""
    before = dict(TOOL_REGISTRY)
    _RUNS.clear()
    for cls in _FAKE_TOOLS:
        register_tool(cls.name)(cls)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(before)
    _RUNS.clear()


def _ctx() -> ToolContext:
    return ToolContext(
        providers=None, detector=None, logger=logging.getLogger("omnia.test")
    )


def _rule(*tools: str, kind: str = "text", **params) -> SmartNotesFieldRule:
    return SmartNotesFieldRule(
        kind=kind,
        target_field="Def",
        tools=tuple(CompiledToolSpec(name=name, params=params) for name in tools),
    )


def _trace(result) -> list[tuple[str, str]]:
    return [(attempt.tool, attempt.status) for attempt in result.attempts]


class TestRegisterTool:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            register_tool("")(_ProduceTool)

    def test_duplicate_name_different_class_raises(self):
        class _Other(_ProduceTool):
            pass

        # "ai" is already bound to AiTool; binding a different class must fail.
        with pytest.raises(ValueError):
            register_tool("ai")(_Other)

    def test_same_class_reregister_is_noop(self):
        before = dict(TOOL_REGISTRY)
        register_tool("ai")(AiTool)
        assert before == TOOL_REGISTRY

    def test_registered_names_sorted(self):
        names = registered_tools()
        assert names == sorted(names)

    def test_builtin_ai_tool_is_registered(self):
        assert get_tool("ai") is AiTool

    def test_get_tool_unknown_returns_none(self):
        assert get_tool("does-not-exist") is None

    def test_resolve_tool_instantiates_the_class(self):
        tool = resolve_tool("ai")
        assert isinstance(tool, AiTool)

    def test_resolve_tool_unknown_returns_none(self):
        assert resolve_tool("does-not-exist") is None


class TestToolsCatalog:
    def test_entries_are_sorted_and_describe_each_tool(self, fake_tools):
        catalog = {entry["name"]: entry for entry in tools_catalog(_ctx())}
        assert list(catalog) == sorted(catalog)
        ai_entry = catalog["ai"]
        assert ai_entry["label"] == "AI"
        assert ai_entry["kinds"] == ["image", "text", "tts"]
        assert ai_entry["deterministic"] is False
        assert ai_entry["params_schema"] is None
        assert ai_entry["unavailable_reason"] is None

    def test_params_schema_and_availability_are_surfaced(self, fake_tools):
        catalog = {entry["name"]: entry for entry in tools_catalog(_ctx())}
        schema = catalog["t_param"]["params_schema"]
        assert schema is not None
        assert set(schema["properties"]) == {"source_field", "suffix"}
        assert catalog["t_unavailable"]["unavailable_reason"] == (
            "needs a WAV TTS provider"
        )


class TestPipelineMatrix:
    def test_declined_then_produced(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_decline", "t_produce"), {})
        assert result.produced is not None
        assert result.produced.text == "made"
        assert _trace(result) == [
            ("t_decline", "not_applicable"),
            ("t_produce", "produced"),
        ]
        assert result.errored is False

    def test_error_then_produced_is_absorbed(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_boom", "t_produce"), {})
        assert result.produced is not None
        # The error stays in the trace, but the field succeeded — so it is NOT a failure.
        assert _trace(result) == [("t_boom", "error"), ("t_produce", "produced")]
        assert result.errored is False

    def test_error_then_declined_is_errored(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_boom", "t_decline"), {})
        assert result.produced is None
        assert result.errored is True
        assert result.summary == "t_boom: kaput; t_decline: no match"

    def test_declined_then_empty_is_unproductive(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_decline", "t_empty"), {})
        assert result.produced is None
        # Nothing broke: every tool simply had nothing to make.
        assert result.errored is False
        assert _trace(result) == [("t_decline", "not_applicable"), ("t_empty", "empty")]

    def test_unknown_tool_falls_through(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_missing", "t_produce"), {})
        assert result.produced is not None
        assert _trace(result) == [
            ("t_missing", "unknown_tool"),
            ("t_produce", "produced"),
        ]
        assert result.attempts[0].detail == "no tool named 't_missing'"

    def test_wrong_kind_falls_through(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_image_only", "t_produce"), {})
        assert result.produced is not None
        assert _trace(result) == [
            ("t_image_only", "wrong_kind"),
            ("t_produce", "produced"),
        ]
        # The tool never ran — the kind gate is checked before it is invoked.
        assert _RUNS == ["t_produce"]

    def test_trace_is_in_execution_order_and_stops_at_the_first_result(
        self, fake_tools
    ):
        rule = _rule("t_decline", "t_empty", "t_boom", "t_produce", "t_decline")
        result = GenerationPipeline(_ctx()).run(rule, {})
        assert _trace(result) == [
            ("t_decline", "not_applicable"),
            ("t_empty", "empty"),
            ("t_boom", "error"),
            ("t_produce", "produced"),
        ]
        # The tool after the winner is never invoked.
        assert _RUNS == ["t_decline", "t_empty", "t_boom", "t_produce"]

    def test_empty_chain_produces_nothing(self, fake_tools):
        rule = SmartNotesFieldRule(kind="text", target_field="Def", tools=())
        result = GenerationPipeline(_ctx()).run(rule, {})
        assert result.produced is None
        assert result.attempts == ()
        assert result.errored is False
        assert result.summary == "no tools configured for this field"

    def test_a_tool_returning_a_non_outcome_is_an_error_attempt(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_junk", "t_produce"), {})
        assert result.produced is not None
        assert _trace(result) == [("t_junk", "error"), ("t_produce", "produced")]
        assert "not a ToolOutcome" in result.attempts[0].detail

    def test_single_attempt_summary_has_no_tool_prefix(self, fake_tools):
        # The legacy one-tool chain must surface the provider's own message verbatim.
        result = GenerationPipeline(_ctx()).run(_rule("t_boom"), {})
        assert result.summary == "kaput"


class TestEveryFailureFallsThroughToTheNextTool:
    """The chain's ONE rule: run the tools in the configured order; a failure moves to the next.

    There is deliberately no exception to this — no tool can halt the chain, and no tool can
    refuse to share it. An earlier design let ``cloze_audio`` stop a chain it could not mask,
    on the grounds that a following tts tool would speak the answer it exists to hide. That
    was reversed: the chain's semantics are the user's to configure, and a rule with one
    special case is one nobody can predict from the picker. The tool's guarantee stays what a
    tool can actually promise — *it* never speaks the answer (see the ``cloze_audio`` module
    docstring for what that does and does not cover).
    """

    def test_a_later_tool_still_runs_after_any_failure(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_terminal", "t_produce"), {})

        assert result.produced is not None
        assert _RUNS == ["t_terminal", "t_produce"]
        assert _trace(result) == [("t_terminal", "error"), ("t_produce", "produced")]

    def test_an_ordinary_error_falls_through_the_same_way(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_boom", "t_produce"), {})

        assert result.produced is not None
        assert _RUNS == ["t_boom", "t_produce"]

    def test_order_is_honoured_so_an_earlier_tool_wins(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_produce", "t_terminal"), {})

        assert result.produced is not None
        assert _RUNS == ["t_produce"]  # the chain stops at the first tool that PRODUCES

    def test_a_lone_failing_tool_keeps_the_note_for_retry(self, fake_tools):
        result = GenerationPipeline(_ctx()).run(_rule("t_terminal"), {})

        assert result.errored is True
        assert result.summary == "would leak the answer"

    def test_the_service_raises_the_failure_as_the_chain_error_cause(self, fake_tools):
        service = GenerationService(providers=None)
        with pytest.raises(ToolChainError) as excinfo:
            service.generate(_rule("t_terminal"), {})
        assert isinstance(excinfo.value.cause, ToolError)

    def test_two_tools_of_the_same_kind_share_a_chain_freely(self, fake_tools):
        # Nothing pre-flights a chain any more: whatever the user ticked, in whatever order,
        # runs. Previously this pair was refused outright before either tool got a turn.
        result = GenerationPipeline(_ctx()).run(_rule("t_terminal", "t_terminal"), {})

        assert _RUNS == ["t_terminal", "t_terminal"]
        assert result.errored is True


class TestTheCatalogDescribesEachToolForThePicker:
    """What the settings page can only learn from the tool itself.

    Both flags exist because the picker must not hardcode knowledge of specific tools: a
    user-authored tool loaded off disk gets the same treatment as a builtin.
    """

    def test_uses_provider_is_not_the_inverse_of_deterministic(self, fake_tools):
        # The distinction the row's Provider/Model/Voice fade depends on. `cloze_audio` is the
        # real case: deterministic (no LLM tokens) AND provider-using (it synthesizes speech).
        catalog = {entry["name"]: entry for entry in tools_catalog(_ctx())}

        assert catalog["t_requires"]["deterministic"] is True
        assert catalog["t_requires"]["uses_provider"] is False
        assert catalog["t_produce"]["uses_provider"] is True  # the default

    def test_required_params_reach_the_picker_so_done_can_refuse(self, fake_tools):
        catalog = {entry["name"]: entry for entry in tools_catalog(_ctx())}

        assert catalog["t_requires"]["required_params"] == ["sentence_field"]
        assert catalog["t_produce"]["required_params"] == []  # the default

    def test_a_tool_with_a_malformed_required_params_costs_only_its_own_validation(
        self, fake_tools
    ):
        # Same defensiveness as `kinds`, and BOTH guards are exercised. A plain `@property`
        # only reaches the isinstance branch — read off the CLASS it returns the property
        # object rather than raising — so the raising case needs a descriptor with a
        # `__get__` that fires on class access too.
        class _Raises:
            def __get__(self, instance, owner=None):
                raise RuntimeError("boom")

        class _Exploding(_ProduceTool):
            name: ClassVar[str] = "t_raises_required"
            required_params = _Raises()  # type: ignore[assignment]

        class _NotIterable(_ProduceTool):
            name: ClassVar[str] = "t_bad_required"

            @property  # type: ignore[misc]
            def required_params(self):
                return frozenset()

        assert tool_required_params(_Exploding) == frozenset()  # the except branch
        assert (
            tool_required_params(_NotIterable) == frozenset()
        )  # the isinstance branch
        assert tool_required_params(_ProduceTool) == frozenset()  # the ordinary default


class TestPipelineIsolatesABrokenTool:
    """A tool that breaks BEFORE it runs must still only cost its own attempt.

    Resolution and the kind gate live inside the pipeline's guard for exactly this reason:
    escaping ``run()`` would abort the whole note in ``generate_note`` and discard the sibling
    fields that already generated.
    """

    def test_a_tool_whose_constructor_raises_becomes_an_error_attempt(self, fake_tools):
        register_tool(_ExplodingInitTool.name)(_ExplodingInitTool)

        result = GenerationPipeline(_ctx()).run(_rule("t_bad_init", "t_produce"), {})

        assert result.produced is not None
        assert result.produced.text == "made"
        assert _trace(result) == [("t_bad_init", "error"), ("t_produce", "produced")]
        assert result.attempts[0].detail == "cannot start"

    def test_a_tool_class_missing_kinds_becomes_an_error_attempt(self, fake_tools):
        register_tool(_NoKindsTool.name)(_NoKindsTool)

        result = GenerationPipeline(_ctx()).run(_rule("t_no_kinds", "t_produce"), {})

        assert result.produced is not None
        assert _trace(result) == [("t_no_kinds", "error"), ("t_produce", "produced")]
        assert "kinds" in result.attempts[0].detail

    def test_a_note_keeps_its_other_fields_when_one_tool_cannot_be_built(
        self, fake_tools
    ):
        register_tool(_ExplodingInitTool.name)(_ExplodingInitTool)
        config = SmartNotesNoteTypeConfig(
            note_type="Basic",
            base_field="Word",
            fields=[
                SmartNotesFieldConfig(
                    field="Def",
                    enabled=True,
                    type="text",
                    prompt="d {{Word}}",
                    tools=[FieldToolConfig(tool="t_bad_init")],
                ),
                SmartNotesFieldConfig(
                    field="Example",
                    enabled=True,
                    type="text",
                    prompt="e {{Word}}",
                    tools=[FieldToolConfig(tool="t_produce")],
                ),
            ],
        )

        results, _blocked, failed = GenerationService(_StubHub()).generate_note(
            config, {"Word": "cat"}
        )

        assert [rule.target_field for rule, _ in results] == ["Example"]
        assert [(item.field, item.kind) for item in failed] == [("Def", "error")]


class TestPipelineParams:
    def test_validated_params_reach_the_tool_with_defaults_filled(self, fake_tools):
        rule = _rule("t_param", source_field="Word")
        result = GenerationPipeline(_ctx()).run(rule, {"Word": "cat"})
        assert result.produced is not None
        assert result.produced.text == "cat!"  # "suffix" defaulted to "!"

    def test_invalid_params_become_an_error_attempt_and_the_chain_continues(
        self, fake_tools
    ):
        rule = SmartNotesFieldRule(
            kind="text",
            target_field="Def",
            tools=(
                CompiledToolSpec(name="t_param", params={}),  # missing source_field
                CompiledToolSpec(name="t_produce"),
            ),
        )
        result = GenerationPipeline(_ctx()).run(rule, {})
        assert result.produced is not None
        assert _trace(result) == [("t_param", "error"), ("t_produce", "produced")]
        assert "source_field" in result.attempts[0].detail
        assert _RUNS == ["t_produce"]  # run() was never reached


class TestAiTool:
    def test_serves_every_generation_kind_and_is_not_deterministic(self):
        assert AiTool.kinds == frozenset({"text", "image", "tts"})
        assert AiTool.deterministic is False
        assert AiTool.params_model is None
        assert AiTool.referenced_fields({}) == []
        assert AiTool.availability(_ctx()) is None


class TestToolCompilation:
    def test_no_configured_tools_compiles_to_the_legacy_ai_chain(self):
        rule = compile_field_rule(
            SmartNotesFieldConfig(field="Def", enabled=True, type="text"), "Word"
        )
        assert rule.tools == default_tool_chain()
        assert [spec.name for spec in rule.tools] == ["ai"]

    def test_a_rule_built_directly_defaults_to_the_ai_chain(self):
        assert [spec.name for spec in SmartNotesFieldRule().tools] == ["ai"]

    def test_configured_tools_compile_in_order_with_copied_params(self):
        config = SmartNotesFieldConfig(
            field="Def",
            enabled=True,
            type="text",
            tools=[
                FieldToolConfig(tool="cloze", params={"sentence_field": "Sentence"}),
                FieldToolConfig(tool="ai"),
            ],
        )
        rule = compile_field_rule(config, "Word")
        assert [spec.name for spec in rule.tools] == ["cloze", "ai"]
        assert rule.tools[0].params == {"sentence_field": "Sentence"}
        # The compiled spec must not alias the persisted config's dict.
        rule.tools[0].params["sentence_field"] = "Other"
        assert config.tools[0].params == {"sentence_field": "Sentence"}

    def test_old_blob_without_tools_parses_to_an_empty_chain(self):
        config = SmartNotesFieldConfig.parse_obj(
            {"field": "Def", "enabled": True, "type": "text", "prompt": "d {{Word}}"}
        )
        assert config.tools == []
        assert "tools" not in config.dict()


class TestToolPrerequisites:
    def test_tool_params_become_hard_prerequisites(self, fake_tools):
        rule = SmartNotesFieldRule(
            target_field="Def",
            prompt="define {{Word}}",
            tools=(
                CompiledToolSpec(name="t_param", params={"source_field": "Sentence"}),
            ),
        )
        assert rule_prerequisites(rule) == [("Word", "hard"), ("Sentence", "hard")]

    def test_an_explicit_dep_recolours_a_tool_edge(self, fake_tools):
        rule = SmartNotesFieldRule(
            target_field="Def",
            tools=(
                CompiledToolSpec(name="t_param", params={"source_field": "Sentence"}),
            ),
            depends_on=[FieldDep(field="sentence", kind="soft")],
        )
        assert rule_prerequisites(rule) == [("Sentence", "soft")]

    def test_an_unknown_tool_contributes_no_prerequisites(self):
        rule = SmartNotesFieldRule(
            target_field="Def",
            tools=(CompiledToolSpec(name="user:gone", params={"source_field": "X"}),),
        )
        assert rule_prerequisites(rule) == []

    def test_ordering_inherits_a_tool_param_edge(self, fake_tools):
        # Def's tool reads Sentence, which is itself generated → Sentence must run first, with
        # no prompt reference between them.
        sentence = SmartNotesFieldRule(target_field="Sentence", prompt="s {{Word}}")
        definition = SmartNotesFieldRule(
            target_field="Def",
            prompt="d {{Word}}",
            tools=(
                CompiledToolSpec(name="t_param", params={"source_field": "Sentence"}),
            ),
        )
        ordered = [rule.target_field for rule in order_rules([definition, sentence])]
        assert ordered == ["Sentence", "Def"]


class _StubHub:
    """A ProviderHub-shaped stub; the fake tools never touch it."""

    def llm(self, **kwargs):  # pragma: no cover - unused by the fake tools
        raise AssertionError("no provider call expected")

    def tts(self, **kwargs):  # pragma: no cover - unused by the fake tools
        raise AssertionError("no provider call expected")


def _note_config(tools: list[FieldToolConfig]) -> SmartNotesNoteTypeConfig:
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Def", enabled=True, type="text", prompt="d {{Word}}", tools=tools
            )
        ],
    )


class TestServiceOnChains:
    def test_generate_raises_a_tool_chain_error_listing_every_attempt(self, fake_tools):
        service = GenerationService(_StubHub())
        with pytest.raises(ProviderError) as excinfo:
            service.generate(_rule("t_decline", "t_boom"), {})
        assert isinstance(excinfo.value, ToolChainError)
        assert str(excinfo.value) == "t_decline: no match; t_boom: kaput"
        assert [a.status for a in excinfo.value.attempts] == ["not_applicable", "error"]

    def test_generate_note_marks_an_all_declined_field_unproductive(self, fake_tools):
        service = GenerationService(_StubHub())
        config = _note_config([FieldToolConfig(tool="t_decline")])
        results, blocked, failed = service.generate_note(config, {"Word": "cat"})
        assert results == []
        assert blocked == []
        assert [(item.field, item.kind, item.error) for item in failed] == [
            ("Def", "unproductive", "no match")
        ]

    def test_generate_note_marks_a_broken_chain_errored(self, fake_tools):
        service = GenerationService(_StubHub())
        config = _note_config(
            [FieldToolConfig(tool="t_boom"), FieldToolConfig(tool="t_decline")]
        )
        _results, _blocked, failed = service.generate_note(config, {"Word": "cat"})
        assert [(item.field, item.kind) for item in failed] == [("Def", "error")]
        assert failed[0].error == "t_boom: kaput; t_decline: no match"

    def test_generate_note_absorbs_an_error_a_later_tool_recovers_from(
        self, fake_tools
    ):
        service = GenerationService(_StubHub())
        config = _note_config(
            [FieldToolConfig(tool="t_boom"), FieldToolConfig(tool="t_produce")]
        )
        results, _blocked, failed = service.generate_note(config, {"Word": "cat"})
        assert [rule.target_field for rule, _ in results] == ["Def"]
        assert failed == []


class TestABrokenToolCostsOnlyItself:
    """The registry's read paths are as guarded as the pipeline's run path.

    ``tool_referenced_fields`` runs while COMPILING a note's rules and ``tools_catalog`` runs
    while opening the dialog, so an exception there would abort the whole note (or the whole
    picker) — the same failure the pipeline guard eliminates. Phase 1 registers only ``ai``,
    but Phase 4 loads user-authored classes off disk, where this is a real risk.
    """

    def test_a_tool_that_cannot_report_its_fields_is_skipped(self, fake_tools):
        class _BoomRefs(_ProduceTool):
            name = "boom_refs"

            @classmethod
            def referenced_fields(cls, params):
                raise RuntimeError("cannot introspect")

        register_tool("boom_refs")(_BoomRefs)

        assert tool_referenced_fields((CompiledToolSpec(name="boom_refs"),)) == []

    def test_a_tool_that_cannot_describe_itself_leaves_the_catalog_usable(
        self, fake_tools
    ):
        class _BoomDesc(_ProduceTool):
            name = "boom_desc"

            @classmethod
            def availability(cls, ctx):
                raise RuntimeError("cannot describe")

        register_tool("boom_desc")(_BoomDesc)

        names = [entry["name"] for entry in tools_catalog(_ctx())]

        assert "boom_desc" not in names
        assert names, "the other tools still describe themselves"


class TestAnExhaustedChainKeepsItsCause:
    """``ToolChainError`` must not erase the exception that actually ended the chain.

    Without this a legacy one-``ai`` chain silently promoted a non-provider bug (a KeyError from
    a bad template, say) into a ``ProviderError``, which the UI prints verbatim — turning
    "Preview failed - see logs." into a raw "'Word'". It also dropped a provider's status_code,
    so 429/5xx handling upstream stopped seeing the code.
    """

    def test_the_cause_is_the_tools_own_exception(self):
        original = KeyError("Word")
        error = ToolChainError((ToolAttempt("ai", "error", "'Word'", error=original),))

        assert error.cause is original

    def test_a_chain_that_only_declined_has_no_cause(self):
        error = ToolChainError((ToolAttempt("cloze", "not_applicable", "no match"),))

        assert error.cause is None


def _tool_hub():
    """The same provider hub the service tests use: fake HTTP, a real TTS path."""
    from test_smart_notes import _hub

    return _hub()


@pytest.fixture
def sound_name_tool():
    """A user tool that pulls the filename out of a ``[sound:…]`` reference."""
    import re as _re

    class _Params(BaseModel):
        source_field: str = ""

    @register_tool("user:sound-name")
    class _SoundName(Tool):
        name: ClassVar[str] = "user:sound-name"
        label: ClassVar[str] = "Sound name"
        description: ClassVar[str] = "Filename out of a [sound:...] reference."
        kinds: ClassVar[frozenset] = frozenset({"text"})
        deterministic: ClassVar[bool] = True
        uses_provider: ClassVar[bool] = False
        params_model: ClassVar[type] = _Params

        @classmethod
        def referenced_fields(cls, params):
            name = str(params.get("source_field", "") or "").strip()
            return [name] if name else []

        def run(self, request, ctx):
            wanted = str(request.params.get("source_field", "") or "").strip()
            value = ""
            for key, val in request.fields.items():
                if key.strip().lower() == wanted.strip().lower():
                    value = str(val)
            if not value.strip():
                return NotApplicable("the source field is empty")
            match = _re.search(r"\[sound:\s*([^\]]+?)\s*\]", value)
            if match is None:
                return NotApplicable("no [sound:] reference in the value")
            return Produced(GenerationResult("text", text=match.group(1)))

    yield _SoundName
    unregister_tool("user:sound-name")


class TestAMediaFieldFeedsTheFieldsThatReadIt:
    """A field reading a generated audio field must see the reference the note will hold.

    Reported from real use: a tool extracting the filename out of ``[sound:…]`` produced nothing
    when the note was generated for real, while Preview looked right. Preview reads the SAVED
    note, where the audio already exists; a real run generated the audio and then handed the
    dependent field a blank source, because media results were deliberately kept out of the
    working map ("embed refs, not prompt text").

    The dependent field was not merely wrong — it was skipped by ``should_skip_rule`` before its
    tools were consulted, so it appeared in neither ``blocked`` nor ``failed``. No output, no
    error, nothing to explain it.
    """

    @staticmethod
    def _config():
        return SmartNotesNoteTypeConfig(
            note_type="AnkiVocabulary",
            base_field="Word",
            enabled=True,
            fields=[
                SmartNotesFieldConfig(
                    field="Definition (audio)",
                    enabled=True,
                    type="tts",
                    prompt="{{Definition}}",
                ),
                SmartNotesFieldConfig(
                    field="Definition (audio filename)",
                    enabled=True,
                    type="text",
                    prompt="{{Definition (audio)}}",
                    tools=[
                        FieldToolConfig(
                            tool="user:sound-name",
                            params={"source_field": "Definition (audio)"},
                        )
                    ],
                ),
            ],
        )

    @staticmethod
    def _fields():
        return {
            "Word": "cowork",
            "Definition": "to work together",
            "Definition (audio)": "",
            "Definition (audio filename)": "",
        }

    def test_the_dependent_field_generates(self, sound_name_tool):
        service = GenerationService(_tool_hub())
        seen: list[str] = []

        def materialize(rule, result):
            seen.append(rule.target_field)
            return f"[sound:omnia-1-{rule.target_field}.mp3]"

        results, blocked, failed = service.generate_note(
            self._config(), self._fields(), materialize=materialize
        )

        produced = {rule.target_field: res for rule, res in results}
        assert "Definition (audio filename)" in produced, (blocked, failed)
        assert produced["Definition (audio filename)"].text == (
            "omnia-1-Definition (audio).mp3"
        )

    def test_media_is_materialized_exactly_once(self, sound_name_tool):
        """The chain and the note must agree on the filename.

        Anki renames on collision, so materializing a second time at write would return a
        DIFFERENT name than the one already handed downstream — and the extracted filename
        would point at a file the note does not reference.
        """
        service = GenerationService(_tool_hub())
        calls: list[str] = []

        def materialize(rule, result):
            calls.append(rule.target_field)
            return f"[sound:omnia-1-{rule.target_field}.mp3]"

        service.generate_note(self._config(), self._fields(), materialize=materialize)

        assert calls == ["Definition (audio)"]

    def test_without_a_materializer_the_old_behaviour_stands(self, sound_name_tool):
        # Callers that pass none (the engine's own tests, any headless use) must not start
        # requiring Anki's media folder just because this seam grew a parameter.
        service = GenerationService(_tool_hub())

        results, _blocked, _failed = service.generate_note(
            self._config(), self._fields()
        )

        assert [rule.target_field for rule, _ in results] == ["Definition (audio)"]
