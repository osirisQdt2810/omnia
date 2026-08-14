"""Tests for user-authored tools: the store, the loader, the review gate, and the runtime.

The load-bearing properties, all pinned here:

* **One bad file costs only itself.** A module that raises at import is skipped and logged, and
  every other file still loads — this runs at plugin start, so anything else would mean a
  broken tool file bricks smart_notes.
* **A user tool cannot shadow a builtin.** Whatever a file's ``@register_tool`` argument says,
  the loader only accepts the name ``user:<its own slug>`` and rolls the rest back.
* **At run time it is just a registered tool.** A loaded tool goes through the REAL
  :class:`GenerationPipeline` — same params validation, same outcome taxonomy, same fall-through
  — and never touches a provider, which is the entire point (no tokens).
* **Nothing is saved unseen.** :class:`ReviewGate` only lets through the exact source that was
  test-run, so editing the code after a test re-arms it.

Every generated file is written into ``tmp_path`` — the repo's own test tree never gains an
importable Python file at collection time.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.authoring import tool_author as tool_author_module
from omnia.plugins.smart_notes.authoring.tool_author import (
    ToolAuthor,
    build_user_tool_message,
    parse_user_tool_reply,
    user_tool_system_prompt,
)
from omnia.plugins.smart_notes.config import (
    CompiledToolSpec,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine.tools import (
    TOOL_REGISTRY,
    GenerationPipeline,
    ImportGuard,
    ReviewGate,
    Tool,
    ToolContext,
    UserToolError,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    UserToolTester,
    get_tool,
    is_user_tool,
    register_tool,
    slugify,
    unregister_tool,
    user_tool_name,
    validate_slug,
)
from omnia.plugins.smart_notes.engine.tools.registry import tool_referenced_fields

# --- sample tool sources ------------------------------------------------------------------
# Written as text (not imported) exactly as a generated tool would arrive: the loader compiles
# a string, so the tests exercise the real path.

_GOOD = '''
from typing import ClassVar

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools import (
    NotApplicable,
    Produced,
    Tool,
    register_tool,
)


class ExtParams(PersistedModel):
    source_field: str = Field("", description="Field holding the filename.")


@register_tool("user:{slug}")
class ExtTool(Tool):
    """Takes the extension out of a filename."""

    name: ClassVar[str] = "user:{slug}"
    label: ClassVar[str] = "Extract extension"
    description: ClassVar[str] = "Output just the file extension."
    kinds: ClassVar[frozenset] = frozenset({{"text"}})
    deterministic: ClassVar[bool] = True
    params_model: ClassVar[type] = ExtParams

    @classmethod
    def referenced_fields(cls, params):
        name = str(params.get("source_field", "") or "").strip()
        return [name] if name else []

    def run(self, request, ctx):
        name = str(request.params.get("source_field", "") or "").strip() or "Sample"
        value = ""
        for key, val in request.fields.items():
            if key.strip().lower() == name.strip().lower():
                value = str(val)
        if "." not in value:
            return NotApplicable("no extension in " + name)
        return Produced(GenerationResult("text", text=value.rsplit(".", 1)[1].strip("] ")))
'''

_RAISES_AT_IMPORT = """
raise RuntimeError("this module explodes while being imported")
"""

_STEALS_A_BUILTIN = '''
from typing import ClassVar

from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools import Produced, Tool, register_tool


@register_tool("ai")
class Impostor(Tool):
    """Tries to take over the builtin AI tool's name."""

    name: ClassVar[str] = "ai"
    label: ClassVar[str] = "Not the AI tool"
    description: ClassVar[str] = "Would shadow the builtin."
    kinds: ClassVar[frozenset] = frozenset({"text"})
    deterministic: ClassVar[bool] = True

    def run(self, request, ctx):
        return Produced(GenerationResult("text", text="hijacked"))
'''

_REGISTERS_NOTHING = """
VALUE = 1
"""


def _good_source(slug: str) -> str:
    """The working tool source, registered under ``slug``."""
    return _GOOD.format(slug=slug)


class _ExplodingProviders:
    """A provider hub that fails the test the moment a tool reaches for a provider."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"a deterministic user tool must not touch providers.{name}"
        )


def _ctx() -> ToolContext:
    """A tool context whose providers cannot be used without failing the test."""
    return ToolContext(
        providers=_ExplodingProviders(),
        detector=None,
        logger=logging.getLogger("omnia.test"),
    )


def _rule(*tools: str, **params) -> SmartNotesFieldRule:
    """A compiled rule whose chain is ``tools``, all sharing ``params``."""
    return SmartNotesFieldRule(
        kind="text",
        target_field="Ext",
        base_field="Sample",
        tools=tuple(CompiledToolSpec(name=name, params=params) for name in tools),
    )


@pytest.fixture
def registry_guard():
    """Restore the tool registry after a test that loads tools into it."""
    before = dict(TOOL_REGISTRY)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(before)


@pytest.fixture
def store(tmp_path):
    """A store over a throwaway directory (never the repo's own tree)."""
    return UserToolStore(tmp_path / "tools")


@pytest.fixture
def loader(store, registry_guard):
    """A loader over the throwaway store, with the registry restored afterwards."""
    return UserToolLoader(store, log=logging.getLogger("omnia.test"))


class TestSlugs:
    def test_slugify_makes_a_file_safe_name(self):
        assert slugify("Extract  the Extension!") == "extract-the-extension"

    def test_slugify_drops_leading_and_trailing_punctuation(self):
        assert slugify("  ...Audio ext...  ") == "audio-ext"

    def test_slugify_of_pure_punctuation_is_empty(self):
        assert slugify("///") == ""

    def test_validate_rejects_a_path_traversal(self):
        with pytest.raises(UserToolError):
            validate_slug("../../evil")

    def test_validate_rejects_upper_case(self):
        with pytest.raises(UserToolError):
            validate_slug("Ext")

    def test_validate_accepts_a_plain_slug(self):
        assert validate_slug("extract-ext") == "extract-ext"

    def test_user_names_are_namespaced(self):
        assert user_tool_name("ext") == "user:ext"
        assert is_user_tool("user:ext")
        assert not is_user_tool("cloze")


class TestUserToolSource:
    def test_the_prompt_round_trips_through_the_file_text(self):
        source = UserToolSource(slug="ext", code="X = 1\n", prompt="take the extension")

        parsed = UserToolSource.parse("ext", source.render())

        assert parsed.prompt == "take the extension"
        assert parsed.code == "X = 1\n"

    def test_a_multiline_prompt_survives(self):
        prompt = "line one\nline two"

        parsed = UserToolSource.parse(
            "ext", UserToolSource(slug="ext", code="X = 1", prompt=prompt).render()
        )

        assert parsed.prompt == prompt

    def test_rerendering_does_not_stack_headers(self):
        first = UserToolSource(slug="ext", code="X = 1", prompt="a").render()

        second = UserToolSource(slug="ext", code=first, prompt="b").render()

        assert second.count("omnia-user-tool:") == 1
        assert UserToolSource.parse("ext", second).prompt == "b"

    def test_a_hand_written_file_without_a_header_still_loads(self):
        parsed = UserToolSource.parse("ext", "X = 1\n")

        assert parsed.prompt == ""
        assert parsed.code == "X = 1\n"

    def test_a_corrupt_header_costs_only_the_prompt(self):
        parsed = UserToolSource.parse("ext", "# omnia-user-tool: {not json\nX = 1\n")

        assert parsed.prompt == ""
        assert parsed.code == "X = 1\n"


class TestImportGuard:
    """The speed bump: it keeps the source the user reads honest about what it does."""

    def test_string_work_is_allowed(self):
        ImportGuard().check("import re\nimport json\n")

    def test_the_tools_seam_is_allowed(self):
        ImportGuard().check(
            "from omnia.plugins.smart_notes.engine.tools import Tool, register_tool\n"
        )

    def test_the_operating_system_is_not(self):
        with pytest.raises(UserToolError, match="may not import 'os'"):
            ImportGuard().check("import os\n")

    def test_a_network_module_is_not(self):
        with pytest.raises(UserToolError, match=r"urllib\.request"):
            ImportGuard().check("from urllib.request import urlopen\n")

    def test_urllib_parse_is_allowed_but_not_its_siblings(self):
        ImportGuard().check("from urllib.parse import quote\n")
        with pytest.raises(UserToolError):
            ImportGuard().check("import urllib.request\n")

    def test_a_relative_import_is_refused(self):
        with pytest.raises(UserToolError, match="relative import"):
            ImportGuard().check("from . import sibling\n")

    def test_opening_a_file_is_flagged(self):
        with pytest.raises(UserToolError, match=r"may not call open\(\)"):
            ImportGuard().check("def run():\n    return open('/etc/passwd').read()\n")

    def test_dunder_import_is_flagged(self):
        with pytest.raises(UserToolError, match="__import__"):
            ImportGuard().check("m = __import__('os')\n")

    def test_a_syntax_error_names_its_line(self):
        with pytest.raises(UserToolError, match="line 2"):
            ImportGuard().check("x = 1\ndef (:\n")


class TestUserToolStore:
    def test_write_then_read_round_trips(self, store):
        store.write(UserToolSource(slug="ext", code="X = 1", prompt="p"))

        assert store.read("ext").code == "X = 1"
        assert store.read("ext").prompt == "p"

    def test_reading_an_absent_tool_is_none(self, store):
        assert store.read("nope") is None

    def test_listing_is_sorted_and_skips_non_slug_files(self, store):
        store.write(UserToolSource(slug="beta", code="X = 1"))
        store.write(UserToolSource(slug="alpha", code="X = 1"))
        (store.directory / "__init__.py").write_text("", encoding="utf-8")
        (store.directory / "notes.txt").write_text("hi", encoding="utf-8")

        assert [source.slug for source in store.list()] == ["alpha", "beta"]

    def test_delete_reports_whether_it_existed(self, store):
        store.write(UserToolSource(slug="ext", code="X = 1"))

        assert store.delete("ext") is True
        assert store.delete("ext") is False

    def test_a_bad_slug_cannot_escape_the_directory(self, store):
        with pytest.raises(UserToolError):
            store.path_for("../outside")


class TestUserToolLoader:
    def test_a_good_tool_registers_under_its_user_name(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))

        loads = loader.load_all()

        assert [(load.slug, load.ok) for load in loads] == [("ext", True)]
        assert get_tool("user:ext") is not None
        assert loader.loaded == ("user:ext",)

    def test_a_module_that_raises_is_skipped_and_logged(self, store, loader, caplog):
        store.write(UserToolSource(slug="boom", code=_RAISES_AT_IMPORT))

        with caplog.at_level(logging.ERROR, logger="omnia.test"):
            loads = loader.load_all()

        assert loads[0].ok is False
        assert "explodes" in loads[0].error
        assert get_tool("user:boom") is None
        assert "boom" in caplog.text

    def test_one_broken_file_does_not_stop_the_others(self, store, loader):
        store.write(UserToolSource(slug="aaa-boom", code=_RAISES_AT_IMPORT))
        store.write(UserToolSource(slug="zzz-good", code=_good_source("zzz-good")))

        loads = {load.slug: load.ok for load in loader.load_all()}

        assert loads == {"aaa-boom": False, "zzz-good": True}
        assert get_tool("user:zzz-good") is not None

    def test_a_user_file_cannot_shadow_a_builtin(self, store, loader):
        store.write(UserToolSource(slug="impostor", code=_STEALS_A_BUILTIN))
        before = get_tool("ai")

        load = loader.load("impostor")

        assert load.ok is False
        assert "'ai' already registered" in load.error
        assert get_tool("ai") is before  # the builtin is untouched
        assert get_tool("user:impostor") is None

    def test_a_user_file_cannot_claim_a_bare_name_at_all(self, store, loader):
        # Not just the builtins: a FREE bare name would collide with a builtin a future
        # release adds, so the namespace rule is "exactly your own user: name, or nothing".
        store.write(
            UserToolSource(
                slug="impostor", code=_STEALS_A_BUILTIN.replace('"ai"', '"helper"')
            )
        )

        load = loader.load("impostor")

        assert load.ok is False
        assert "must register exactly 'user:impostor'" in load.error
        assert get_tool("helper") is None

    def test_a_registration_that_is_not_a_tool_is_refused(self, store, loader):
        store.write(
            UserToolSource(
                slug="notatool",
                code=(
                    "from omnia.plugins.smart_notes.engine.tools import register_tool\n"
                    'register_tool("user:notatool")(object())\n'
                ),
            )
        )

        load = loader.load("notatool")

        assert load.ok is False
        assert "not a Tool subclass" in load.error
        assert get_tool("user:notatool") is None

    def test_a_file_that_registers_nothing_is_an_error(self, store, loader):
        store.write(UserToolSource(slug="empty", code=_REGISTERS_NOTHING))

        load = loader.load("empty")

        assert load.ok is False
        assert "registered nothing" in load.error

    def test_a_disallowed_import_is_refused_at_load_not_only_at_save(
        self, store, loader
    ):
        store.write(UserToolSource(slug="sneaky", code="import os\n"))

        load = loader.load("sneaky")

        assert load.ok is False
        assert "may not import 'os'" in load.error

    def test_reloading_an_edited_file_rebinds_the_name(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()
        first = get_tool("user:ext")
        store.write(
            UserToolSource(
                slug="ext", code=_good_source("ext").replace("Extract", "Take")
            )
        )

        loader.load_all()

        assert get_tool("user:ext") is not first
        assert get_tool("user:ext").label == "Take extension"

    def test_a_deleted_file_is_unregistered_on_the_next_load(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()
        store.delete("ext")

        loader.load_all()

        assert get_tool("user:ext") is None
        assert loader.loaded == ()

    def test_unload_all_removes_exactly_what_it_registered(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        loader.unload_all()

        assert get_tool("user:ext") is None
        assert get_tool("cloze") is not None  # builtins are untouched

    def test_compiling_does_not_register_globally(self, store, loader):
        source = UserToolSource(slug="ext", code=_good_source("ext"))

        cls = loader.compile_tool(source)

        assert cls.name == "user:ext"
        assert get_tool("user:ext") is None


class TestUserToolRuntime:
    """A loaded user tool is a registered tool — nothing about the pipeline knows otherwise."""

    def test_it_runs_through_the_real_pipeline_without_touching_a_provider(
        self, store, loader
    ):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        result = GenerationPipeline(_ctx()).run(
            _rule("user:ext", source_field="Audio"),
            {"Audio": "[sound:omnia-1-Audio.mp3]"},
        )

        assert result.produced.text == "mp3"
        assert [(a.tool, a.status) for a in result.attempts] == [
            ("user:ext", "produced")
        ]

    def test_the_produced_result_is_stamped_with_the_tool(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        result = GenerationPipeline(_ctx()).run(
            _rule("user:ext", source_field="Audio"), {"Audio": "clip.wav"}
        )

        assert result.produced.tool == "user:ext"

    def test_declining_falls_through_to_the_next_tool(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        class _Fallback(Tool):
            name: ClassVar[str] = "t_fallback"
            label: ClassVar[str] = "Fallback"
            description: ClassVar[str] = "Always produces."
            kinds: ClassVar[frozenset[str]] = frozenset({"text"})
            deterministic: ClassVar[bool] = True

            def run(self, request, ctx):
                from omnia.plugins.smart_notes.engine.generators import GenerationResult
                from omnia.plugins.smart_notes.engine.tools import Produced

                return Produced(GenerationResult("text", text="fallback"))

        register_tool("t_fallback")(_Fallback)
        try:
            result = GenerationPipeline(_ctx()).run(
                _rule("user:ext", "t_fallback", source_field="Audio"),
                {"Audio": "no-extension-here"},
            )
        finally:
            unregister_tool("t_fallback")

        assert result.produced.text == "fallback"
        assert [a.status for a in result.attempts] == ["not_applicable", "produced"]

    def test_a_missing_user_tool_degrades_to_unknown_tool(self):
        result = GenerationPipeline(_ctx()).run(_rule("user:never-installed"), {})

        assert result.produced is None
        assert result.attempts[0].status == "unknown_tool"

    def test_its_params_feed_the_dependency_graph_like_a_builtin(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        referenced = tool_referenced_fields(
            [CompiledToolSpec(name="user:ext", params={"source_field": "Audio"})]
        )

        assert referenced == ["Audio"]

    def test_its_params_model_validates_like_a_builtin(self, store, loader):
        store.write(UserToolSource(slug="ext", code=_good_source("ext")))
        loader.load_all()

        assert get_tool("user:ext").parse_params({}) == {"source_field": ""}


class TestUserToolTester:
    """The dialog's test box: run the candidate once, describe what came back."""

    def test_a_produced_result_is_reported_with_its_text(self, loader):
        cls = loader.compile_tool(UserToolSource(slug="ext", code=_good_source("ext")))

        result = UserToolTester().run(cls, sample="clip.mp3", params={}, ctx=_ctx())

        assert result.ok is True
        assert result.output == "mp3"

    def test_a_decline_is_reported_with_its_reason(self, loader):
        cls = loader.compile_tool(UserToolSource(slug="ext", code=_good_source("ext")))

        result = UserToolTester().run(cls, sample="no extension", params={}, ctx=_ctx())

        assert result.ok is False
        assert result.status == "not_applicable"
        assert "no extension" in result.detail

    def test_a_raise_is_a_result_too(self, loader):
        code = _good_source("ext").replace(
            'if "." not in value:', 'raise ValueError("kaput")\n        if False:'
        )
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        result = UserToolTester().run(cls, sample="clip.mp3", params={}, ctx=_ctx())

        assert result.status == "error"
        assert result.detail == "kaput"

    def test_a_tool_serving_no_known_kind_cannot_be_tested(self, loader):
        code = _good_source("ext").replace('frozenset({"text"})', "frozenset()")
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        with pytest.raises(UserToolError, match="no generation kind"):
            UserToolTester().run(cls, sample="clip.mp3", params={}, ctx=_ctx())


class TestReviewGate:
    def test_untested_source_is_not_saveable(self):
        assert ReviewGate().is_tested("X = 1") is False

    def test_a_tested_source_passes(self):
        gate = ReviewGate()
        gate.mark_tested("X = 1")

        assert gate.is_tested("X = 1") is True

    def test_editing_after_the_test_re_arms_the_gate(self):
        gate = ReviewGate()
        gate.mark_tested("X = 1")

        assert gate.is_tested("X = 2") is False


class TestToolUsage:
    """Deleting a tool must be able to name the fields it leaves behind."""

    def _settings(self) -> SmartNotesSettings:
        return SmartNotesSettings(
            note_types=[
                SmartNotesNoteTypeConfig(
                    note_type="Vocab",
                    base_field="Word",
                    fields=[
                        SmartNotesFieldConfig(
                            field="Audio Ext",
                            tools=[FieldToolConfig(tool="user:ext")],
                        ),
                        SmartNotesFieldConfig(
                            field="Meaning", tools=[FieldToolConfig(tool="ai")]
                        ),
                    ],
                ),
                SmartNotesNoteTypeConfig(
                    note_type="Phrases",
                    fields=[
                        SmartNotesFieldConfig(
                            field="Ext",
                            tools=[
                                FieldToolConfig(tool="user:ext"),
                                FieldToolConfig(tool="ai"),
                            ],
                        )
                    ],
                ),
            ]
        )

    def test_every_referencing_field_is_reported(self):
        usages = self._settings().fields_using_tool("user:ext")

        assert [str(usage) for usage in usages] == [
            "Vocab · Audio Ext",
            "Phrases · Ext",
        ]

    def test_an_unused_tool_reports_nothing(self):
        assert self._settings().fields_using_tool("user:other") == []


class _FakeLLM:
    """An LLM that returns a canned reply and records the call."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def generate_text(self, prompt, *, system="", temperature=None, **kwargs):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature}
        )
        return self.reply


class TestTheWorkedExampleIsReal:
    """The example in the system prompt is what the model copies — so it must actually work.

    A model that follows the instruction to the letter must not then be rejected by the guard
    or fail to register: these two tests are what keep the prompt and the loader in step.
    """

    def test_it_passes_the_import_guard(self):
        ImportGuard().check(tool_author_module._EXAMPLE)

    def test_the_test_box_can_run_it_with_no_params_configured(self, loader):
        # The sample field is also the rule's only prompt ref, so a tool that defaults its
        # source to "the field this rule reads" finds the sample — the path the dialog's test
        # box actually takes, since it configures no params.
        cls = loader.compile_tool(
            UserToolSource(slug="extract-ext", code=tool_author_module._EXAMPLE)
        )

        result = UserToolTester().run(
            cls, sample="[sound:clip.mp3]", params={}, ctx=_ctx()
        )

        assert result.ok is True
        assert result.output == "mp3"

    def test_it_loads_and_runs_as_a_real_tool(self, store, loader):
        store.write(
            UserToolSource(slug="extract-ext", code=tool_author_module._EXAMPLE)
        )

        loads = loader.load_all()

        assert [(load.slug, load.ok) for load in loads] == [("extract-ext", True)]
        result = GenerationPipeline(_ctx()).run(
            SmartNotesFieldRule(
                kind="text",
                target_field="Ext",
                base_field="Audio",
                tools=(CompiledToolSpec(name="user:extract-ext"),),
            ),
            {"Audio": "[sound:omnia-1-Audio.mp3]"},
        )
        assert result.produced.text == "mp3"


class TestToolAuthor:
    """The ONE LLM call: it happens at authoring time, never at generation time."""

    def test_the_system_prompt_states_the_import_allowlist(self):
        system = user_tool_system_prompt()

        assert "omnia.plugins.smart_notes.engine.tools" in system
        for module in sorted(ImportGuard.ALLOWED_MODULES):
            assert module in system

    def test_the_message_pins_the_registered_name(self):
        message = build_user_tool_message(
            "ext", "take the extension", ["Word", "Audio"]
        )

        assert '"user:ext"' in message
        assert "take the extension" in message
        assert "Word, Audio" in message

    def test_a_fenced_reply_is_unwrapped(self):
        code = parse_user_tool_reply(
            '```python\n@register_tool("user:ext")\nclass T: pass\n```', "ext"
        )

        assert code.startswith("@register_tool")
        assert "```" not in code

    def test_a_reply_registering_the_wrong_name_is_rejected(self):
        with pytest.raises(ProviderError, match="user:ext"):
            parse_user_tool_reply('@register_tool("ai")\n', "ext")

    def test_an_empty_reply_is_rejected(self):
        with pytest.raises(ProviderError, match="no code"):
            parse_user_tool_reply("   ", "ext")

    def test_generate_asks_once_and_returns_the_source(self):
        llm = _FakeLLM('@register_tool("user:ext")\nclass T: pass\n')

        code = ToolAuthor(llm).generate("ext", "take the extension")

        assert len(llm.calls) == 1
        assert "user:ext" in code

    def test_generate_refuses_an_empty_description(self):
        llm = _FakeLLM("")

        with pytest.raises(ProviderError, match="describe what the tool"):
            ToolAuthor(llm).generate("ext", "  ")

        assert llm.calls == []


class TestAMalformedToolClassCostsOnlyItsField:
    """Reading a class attribute stopped being safe when the registry gained non-repo classes.

    ``getattr(cls, "kinds", frozenset())`` only swallows AttributeError. The likelier slip in
    generated code is ``kinds`` written as a ``@property``: the attribute EXISTS, so getattr
    returns the property object and ``kind in cls.kinds`` raises TypeError — out of
    ``chain_conflict``, which ran before the pipeline's per-attempt guard, taking the whole note
    and every sibling field that had already generated with it.
    """

    @pytest.mark.parametrize("shape", ["property", "string", "raises"])
    def test_tool_kinds_reads_a_broken_class_as_empty(self, shape):
        from omnia.plugins.smart_notes.engine.tools.registry import tool_kinds

        class _Property:
            @property
            def kinds(self):
                return frozenset({"text"})

        class _String:
            kinds = "text"  # `"tts" in "text"` is a substring test, not membership

        class _Raises:
            @property
            def kinds(self):
                raise RuntimeError("nope")

        cls = {"property": _Property, "string": _String, "raises": _Raises}[shape]

        assert tool_kinds(cls) == frozenset()

    def test_a_good_class_is_unaffected(self):
        from omnia.plugins.smart_notes.engine.tools.registry import tool_kinds

        class _Good:
            kinds = frozenset({"text", "tts"})

        assert tool_kinds(_Good) == frozenset({"text", "tts"})


class TestTeardownIsKeyedOnTheNamespace:
    """More than one loader exists per process; the registry is the shared truth.

    The plugin builds a loader at enable and the settings dialog builds another when it opens.
    Iterating a per-instance set meant a tool authored in the dialog survived disabling the
    feature, because the plugin's loader had never heard of it — a plugin must leave no trace.
    """

    def test_unload_all_drops_a_tool_another_loader_registered(self, tmp_path):
        from omnia.plugins.smart_notes.engine.tools.base import Empty
        from omnia.plugins.smart_notes.engine.tools.registry import (
            TOOL_REGISTRY,
            get_tool,
            register_tool,
        )
        from omnia.plugins.smart_notes.engine.tools.user_tools import (
            UserToolLoader,
            UserToolStore,
        )

        before = dict(TOOL_REGISTRY)
        try:

            class _Authored(Tool):
                name = "user:authored-elsewhere"
                label = "Authored elsewhere"
                description = ""
                kinds = frozenset({"text"})

                def run(self, request, ctx):
                    return Empty("")

            register_tool("user:authored-elsewhere")(_Authored)
            # A DIFFERENT loader instance — the plugin's, which never loaded that file.
            plugin_loader = UserToolLoader(UserToolStore(tmp_path / "tools"))

            plugin_loader.unload_all()

            assert get_tool("user:authored-elsewhere") is None
        finally:
            TOOL_REGISTRY.clear()
            TOOL_REGISTRY.update(before)

    def test_a_builtin_is_never_dropped(self, tmp_path):
        from omnia.plugins.smart_notes.engine.tools.registry import get_tool
        from omnia.plugins.smart_notes.engine.tools.user_tools import (
            UserToolLoader,
            UserToolStore,
        )

        UserToolLoader(UserToolStore(tmp_path / "tools")).unload_all()

        assert get_tool("ai") is not None
        assert get_tool("cloze") is not None
