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
import re
from typing import ClassVar

import pytest

from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.authoring import tool_author as tool_author_module
from omnia.plugins.smart_notes.authoring.tool_author import (
    CANNOT_MARKER,
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
    INPUT_KIND_EXTENSIONS,
    SAMPLE_FIELD,
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
    declared_inputs,
    get_tool,
    is_user_tool,
    register_tool,
    risky_operations,
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

    def test_the_filesystem_and_processes_are_allowed_now(self):
        """Opened deliberately (see ImportGuard's docstring).

        A transform that CONVERTS rather than rewrites — pull the audio out of a video, resize
        a picture — cannot be written without touching a file. Refusing these made the tool
        author produce code that renamed a string and looked like it worked, which is worse
        than either allowing it or refusing the request outright.

        The guard is a speed bump, not a sandbox; an Anki add-on already has unrestricted
        Python and this dialog always said so. What still holds the line is the mandatory
        read-and-run review, now told what the tool reaches for (see risky_operations).
        """
        for module in (
            "os",
            "subprocess",
            "pathlib",
            "shutil",
            "io",
            "tempfile",
            "wave",
        ):
            ImportGuard().check(f"import {module}\n")

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

    def test_opening_a_file_is_allowed_now(self):
        # A tool that converts a file has to open it. The reader is told, rather than the tool
        # being stopped — see test_the_filesystem_and_processes_are_allowed_now.
        ImportGuard().check("def run():\n    return open('clip.mp4', 'rb').read()\n")

    def test_building_code_from_a_string_is_still_refused(self):
        # These stay out for a DIFFERENT reason than the filesystem ever was: they defeat the
        # one guarantee the guard still makes — that the source the user read is the source
        # that runs. Widening the imports does not widen this.
        for call in ("eval", "exec", "compile", "__import__"):
            with pytest.raises(UserToolError):
                ImportGuard().check(f"x = {call}('1')\n")

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
        # Still enforced at LOAD, not just at save: a file edited by hand after review must not
        # gain a capability silently. `socket` stands in for the modules still outside the list
        # now that the filesystem ones are in.
        store.write(UserToolSource(slug="sneaky", code="import socket\n"))

        load = loader.load("sneaky")

        assert load.ok is False
        assert "may not import 'socket'" in load.error

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


class TestDeclaredInputs:
    """The Try-it form is read from the draft's SOURCE, never by executing it.

    Compiling is the only other way to reach ``Tool.input_kinds``, and compiling means ``exec``.
    In this feature arbitrary execution happens exactly once — on Run, AFTER the risk banner has
    told the user what the code reaches for — so discovering the inputs by compiling would move
    execution ahead of the review that exists to precede it.
    """

    def _declaring(self, mapping: str) -> str:
        """The working tool source with ``input_kinds`` set to ``mapping``."""
        return _good_source("ext").replace(
            "    params_model: ClassVar[type] = ExtParams",
            f"    params_model: ClassVar[type] = ExtParams\n    input_kinds = {mapping}",
        )

    def test_the_declared_fields_are_returned_in_order(self):
        source = self._declaring('{"Clip": "audio", "Word": "text", "Pic": "image"}')

        inputs = declared_inputs(source)

        assert [(item.field, item.kind) for item in inputs] == [
            ("Clip", "audio"),
            ("Word", "text"),
            ("Pic", "image"),
        ]

    def test_an_unknown_kind_falls_back_to_text(self):
        # A typo in generated code costs a typed box, nothing more — and that box can still be
        # handed a file, so an unrecognised kind is not a dead end either.
        inputs = declared_inputs(self._declaring('{"Clip": "sound"}'))

        assert [(item.field, item.kind) for item in inputs] == [("Clip", "text")]

    def test_a_tool_declaring_nothing_gets_one_text_input(self):
        # Byte-for-byte the form this panel offered before it could read a declaration, which is
        # what keeps every tool authored until now exactly as testable as it was.
        inputs = declared_inputs(_good_source("ext"))

        assert [(item.field, item.kind) for item in inputs] == [("Sample", "text")]

    def test_an_empty_mapping_gets_one_text_input(self):
        inputs = declared_inputs(self._declaring("{}"))

        assert [(item.field, item.kind) for item in inputs] == [("Sample", "text")]

    def test_a_computed_mapping_falls_back_to_one_text_input(self):
        # The stated cost of not exec-ing: a mapping this reader cannot describe gets the
        # fallback form, and half a form would be worse than that.
        source = self._declaring('{name: "audio" for name in ("Clip",)}')

        assert [(item.field, item.kind) for item in declared_inputs(source)] == [
            ("Sample", "text")
        ]

    def test_a_module_level_name_does_not_shadow_the_class_declaration(self):
        """``input_kinds`` is a ClassVar, so only a class body may answer for it.

        ``ast.walk`` is breadth-first: a module-level assignment of the same name is reached
        FIRST, and reading it would silently cost a media tool every one of its file pickers
        while the tool's real declaration sat two lines further down.
        """
        source = "input_kinds = {}\n" + self._declaring('{"Clip": "audio"}')

        assert [(item.field, item.kind) for item in declared_inputs(source)] == [
            ("Clip", "audio")
        ]

    def test_a_broken_source_falls_back_instead_of_raising(self):
        # The form is rebuilt on a keystroke debounce, so half-typed code is the NORMAL state.
        assert [
            (item.field, item.kind)
            for item in declared_inputs("class T:\n  input_kinds")
        ] == [("Sample", "text")]

    def test_reading_the_inputs_never_executes_the_module(self, tmp_path, loader):
        """The load-bearing safety property of the whole Try-it form.

        The sentinel is proven live by compiling the SAME source afterwards: if the write never
        happened either way the first assertion would pass for the wrong reason.
        """
        sentinel = tmp_path / "ran"
        code = (
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('yes')\n"
            + self._declaring('{"Clip": "audio"}')
        )

        inputs = declared_inputs(code)

        assert not sentinel.exists()
        assert [(item.field, item.kind) for item in inputs] == [("Clip", "audio")]
        # …and the same source DOES write it when it is actually executed.
        loader.compile_tool(UserToolSource(slug="ext", code=code))
        assert sentinel.read_text() == "yes"


class TestUserToolTester:
    """The dialog's test box: run the candidate once, describe what came back."""

    def test_a_produced_result_is_reported_with_its_text(self, loader):
        cls = loader.compile_tool(UserToolSource(slug="ext", code=_good_source("ext")))

        result = UserToolTester().run(
            cls, inputs={"Sample": "clip.mp3"}, params={}, ctx=_ctx()
        )

        assert result.ok is True
        assert result.output == "mp3"

    def test_a_decline_is_reported_with_its_reason(self, loader):
        cls = loader.compile_tool(UserToolSource(slug="ext", code=_good_source("ext")))

        result = UserToolTester().run(
            cls, inputs={"Sample": "no extension"}, params={}, ctx=_ctx()
        )

        assert result.ok is False
        assert result.status == "not_applicable"
        assert "no extension" in result.detail

    def test_a_raise_is_a_result_too(self, loader):
        code = _good_source("ext").replace(
            'if "." not in value:', 'raise ValueError("kaput")\n        if False:'
        )
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        result = UserToolTester().run(
            cls, inputs={"Sample": "clip.mp3"}, params={}, ctx=_ctx()
        )

        assert result.status == "error"
        assert result.detail == "kaput"

    def test_a_tool_serving_no_known_kind_cannot_be_tested(self, loader):
        code = _good_source("ext").replace('frozenset({"text"})', "frozenset()")
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        with pytest.raises(UserToolError, match="no generation kind"):
            UserToolTester().run(
                cls, inputs={"Sample": "clip.mp3"}, params={}, ctx=_ctx()
            )

    def test_each_declared_input_becomes_a_note_field(self, loader):
        # A tool reading a field by NAME must find the value the form collected for that name,
        # not a single undifferentiated sample.
        code = _good_source("ext").replace('or "Sample"', 'or "Clip"')
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        result = UserToolTester().run(
            cls,
            inputs={"Word": "ignored", "Clip": "[sound:take.wav]"},
            params={},
            ctx=_ctx(),
        )

        assert result.ok is True
        assert result.output == "wav"

    def test_the_rule_names_the_first_input(self, loader):
        # A tool with a blank `<something>_field` param reads the rule's first source field.
        # That has to be the FIRST declared input, or the fallback finds nothing. The prompt
        # now asks for a real default, so the blank case is built here rather than borrowed
        # from the worked example — the runtime still has to honour it (a tool synced from an
        # older Omnia, or one whose param the user cleared).
        blank_default = tool_author_module._EXAMPLE.replace(
            '"Audio", description=', '"", description='
        )
        cls = loader.compile_tool(
            UserToolSource(slug="extract-ext", code=blank_default)
        )

        result = UserToolTester().run(
            cls,
            inputs={"Clip": "[sound:take.wav]", "Word": "ignored"},
            params={},
            ctx=_ctx(),
        )

        assert result.output == "wav"

    def test_a_field_the_form_never_offered_is_named_in_the_result(self, loader):
        """A declaration keyed by a name the tool does not read fails INVISIBLY otherwise.

        The panel builds its rows from ``input_kinds``, so a mis-keyed one renders a plausible,
        correctly-filtered control; the user picks a real file for it; the tool reads the field
        its params actually name, finds nothing, and declines. Every visible signal says the
        pick worked, and the result is "it produced nothing" with no cause anywhere.
        """
        code = _good_source("ext").replace(
            'Field("", description', 'Field("Clip", description'
        )
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        result = UserToolTester().run(
            cls, inputs={"Audio": "[sound:take.wav]"}, params={}, ctx=_ctx()
        )

        assert result.ok is False
        assert "Clip" in result.detail
        assert "input_kinds" in result.detail

    def test_a_tool_reading_only_offered_fields_is_left_alone(self, loader):
        code = _good_source("ext").replace(
            'Field("", description', 'Field("Clip", description'
        )
        cls = loader.compile_tool(UserToolSource(slug="ext", code=code))

        result = UserToolTester().run(
            cls, inputs={"Clip": "take"}, params={}, ctx=_ctx()
        )

        assert result.detail == "no extension in Clip"  # …and nothing appended

    def test_an_empty_input_map_still_runs_under_sample(self, loader):
        # A stale page, or a tool that declares nothing: the run must still happen.
        cls = loader.compile_tool(UserToolSource(slug="ext", code=_good_source("ext")))

        result = UserToolTester().run(cls, inputs={}, params={}, ctx=_ctx())

        assert result.status == "not_applicable"  # nothing typed, so nothing to extract


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
        """The form the example's OWN declaration produces is a form that can run it.

        The panel builds one control per ``input_kinds`` entry and configures no params, so the
        declared field name and the param's default have to be the same name. When they are
        not, the user fills in the only box on the panel and the tool reads nothing — and the
        example is the shape every generated tool imitates.
        """
        inputs = declared_inputs(tool_author_module._EXAMPLE)
        cls = loader.compile_tool(
            UserToolSource(slug="extract-ext", code=tool_author_module._EXAMPLE)
        )

        result = UserToolTester().run(
            cls,
            inputs={item.field: "[sound:clip.mp3]" for item in inputs},
            params={},
            ctx=_ctx(),
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


class TestRiskyOperations:
    """What the review screen is told a tool reaches for.

    The import allowlist stopped being the safety boundary the moment it had to permit ``os``
    and ``subprocess``; the mandatory read-and-run review is. A review is only worth something
    if the reader knows what to look for, and "spot the subprocess import in forty lines of
    generated Python" is not a fair ask when the Python was written by a model and is read
    once.
    """

    def test_a_text_only_tool_reports_nothing(self):
        assert risky_operations("import re\nfrom typing import ClassVar\n") == []

    def test_files_and_processes_are_named_in_plain_language(self):
        found = risky_operations("from pathlib import Path\nimport subprocess\n")

        assert "reads and writes files" in found
        assert "runs other programs on your computer" in found

    def test_each_risk_is_named_once(self):
        found = risky_operations("import os\nimport shutil\nfrom pathlib import Path\n")

        assert len(found) == len(set(found))

    def test_a_bare_open_is_named_even_with_no_imports(self):
        """The one call this guard STOPPED refusing, and the walk could not see.

        `open` is a builtin, so an import walk misses it entirely. A tool doing
        `open(folder + "/" + name, "wb")` with no imports at all raised no banner — and since
        the banner appearing is the signal, its absence affirmatively told the reader "this
        only reshapes text" while the tool truncated a file on disk. Dropping `open` from
        FLAGGED_CALLS was justified by "the review is told instead", so this is that promise.
        """
        assert risky_operations("def r():\n    return open('c.mp4','rb').read()\n") == [
            "reads and writes files"
        ]
        assert risky_operations("def r():\n    open('o.mp3','wb').write(b'x')\n") == [
            "reads and writes files"
        ]

    def test_a_dotted_import_matches_by_longest_prefix(self):
        # Both directions: `import os.path` must find the `os` entry, and
        # `from omnia.core.audio.sidecar import ...` must find `omnia.core.audio` rather than
        # filing every omnia import under a bare first segment.
        assert risky_operations("import os.path\n") == [
            "reads and changes files and folders, and can run other programs"
        ]
        assert (
            "audio"
            in risky_operations("from omnia.core.audio.sidecar import AudioSidecar\n")[
                0
            ]
        )
        # …and an omnia helper that touches nothing still reports nothing.
        assert risky_operations("from omnia.core.lang.text import strip_markup\n") == []

    def test_a_syntax_error_is_left_to_the_guard(self):
        # ImportGuard reports it properly, with the line; this must not raise on the way.
        assert risky_operations("def (") == []


class TestMediaAccess:
    """A tool can find the collection's media folder — the missing half of a conversion tool.

    Output already worked (``GenerationResult(data=..., ext=...)`` is how ``cloze_audio``
    writes a clip). What no tool could do was find the INPUT: a note stores only the bare
    filename, and deriving the folder per-tool means guessing a per-platform profile path.
    """

    def test_the_default_is_empty_rather_than_an_exception(self):
        # Tools call `ctx.media_dir()`; "" is a value they can test and decline on. Raising
        # would take the field down in any headless build or test.
        assert ToolContext(providers=None, detector=None, logger=None).media_dir() == ""

    def test_the_folder_is_injected_as_a_callable(self):
        # A callable, not a string: the context is built on the Qt main thread while a tool
        # runs on a worker, so resolving the collection at build time would touch Anki early
        # and break every headless test of the service.
        ctx = ToolContext(
            providers=None, detector=None, logger=None, media_dir=lambda: "/m"
        )

        assert ctx.media_dir() == "/m"


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

    def test_the_prompt_teaches_the_media_path_it_used_to_forbid(self):
        """The refusal list has to shrink as the capability grows, or it lies.

        The previous change taught the model to answer CANNOT for anything needing a file or a
        transcode — correct while a tool could not import pathlib. Now it can, and a model
        still refusing would be just as wrong as one inventing a renamer.
        """
        prompt = user_tool_system_prompt()

        assert "ctx.media_dir()" in prompt
        assert (
            "ctx.audio" in prompt
        )  # the runtime, rather than a hand-rolled ffmpeg call
        # …and the refusal is still reachable for what remains genuinely impossible.
        assert CANNOT_MARKER in prompt
        assert "NETWORK" in prompt

    def test_the_media_rules_do_not_bias_toward_one_conversion(self):
        """The capability is described, not a recipe.

        This is the same anchoring that made the tool author answer every request with a
        variant of its one worked example, reappearing a level up: a prompt that spells out
        "mp4 -> mp3 is encode(decode(bytes))" and hardcodes ext='mp3' pulls an image resize, a
        frame grab or a plain text transform toward audio. The request decides the shape; the
        prompt only lists what is available.
        """
        prompt = user_tool_system_prompt()

        assert "mp4" not in prompt
        assert "ext='mp3'" not in prompt
        assert "building blocks, not a" in prompt
        assert (
            "It does audio ONLY" in prompt
        )  # scoped, so nothing else is assumed audio

    def test_the_worked_example_is_framed_as_arbitrary(self):
        # One example is an anchor unless it is explicitly labelled as one instance among many.
        prompt = user_tool_system_prompt()

        assert "ONE arbitrary instance" in prompt
        assert "pattern-matches the example" in prompt

    def test_the_prompt_names_the_kind_a_media_tool_must_declare(self):
        """A tool returning bytes under kind 'text' fails SILENTLY and destructively.

        The prompt taught the model to produce media bytes while still mandating
        `kinds = frozenset({"text"})` and `GenerationResult('text', ...)`. The test run then
        looks successful — the tester prints "(text: 55296 bytes of .mp3)" — and on a real note
        `materialize` branches on kind, so 'text' returns `result.text or ""` and the field is
        set to EMPTY. The near miss is no better: the right kind with `kinds={"text"}` makes
        the pipeline skip the tool as wrong_kind on every sound field it was written for.
        """
        prompt = user_tool_system_prompt()

        for token in ("'text'", "'tts'", "'image'"):
            assert token in prompt, token
        assert "wrong_kind" in prompt  # the near miss is called out too
        assert "EMPTY field" in prompt
        # …and nothing mandates text any more.
        assert 'kinds = frozenset({"text"}), deterministic' not in prompt

    def test_the_prompt_no_longer_claims_files_are_forbidden(self):
        prompt = user_tool_system_prompt()

        assert "read or write files" not in prompt

    def test_the_worked_example_declares_the_inputs_its_form_is_built_from(self):
        """The example is the strongest signal in the prompt, so it must teach the right shape.

        Asserted on what ``declared_inputs`` READS out of it rather than on the prose around it:
        a rewording of rule 3b must not fail this, and an example that quietly stops declaring
        anything must.
        """
        inputs = declared_inputs(tool_author_module._EXAMPLE)

        assert [(item.field, item.kind) for item in inputs] == [(SAMPLE_FIELD, "text")]

    def test_the_example_keeps_its_blank_default_and_declares_under_sample(self):
        """The two halves have to agree, and the earlier fix broke them apart.

        Giving ``input_kinds`` a name to key by is not a reason to invent a default field name:
        ``source_field=""`` is what makes the tool follow whatever field the rule points at, and
        baking a literal name in silently overrides that on every note the tool ever runs on.
        The implicit input is declared under ``SAMPLE_FIELD`` instead — the name the form gives
        it — so the picker still knows the kind while the runtime keeps the fallback.

        Pinned because the two are in different parts of the file and nothing else couples them.
        """
        example = tool_author_module._EXAMPLE

        assert 'source_field: str = Field(\n        ""' in example
        assert declared_inputs(example)[0].field == SAMPLE_FIELD

    def test_the_media_shape_the_prompt_teaches_yields_a_file_picker(self):
        """Rule 3b's worked snippet, put through the reader that builds the form.

        The instruction is only worth what the code does with it: a model that follows the
        snippet must get a row whose control is a browser filtered to audio. Greping the prompt
        for its own wording proves nothing about that — this compiles the shape the rule
        describes and asks the reader what form it produces.
        """
        prompt = user_tool_system_prompt()
        snippet = prompt[prompt.index("3b.") : prompt.index("\n4. ")]
        declaration = re.search(r"input_kinds: ClassVar\[.+?\] = (\{[^}]*\})", snippet)
        assert declaration is not None, snippet
        source = _good_source("clip").replace(
            "    params_model: ClassVar[type] = ExtParams",
            "    params_model: ClassVar[type] = ExtParams\n"
            f"    input_kinds = {declaration.group(1)}",
        )

        inputs = declared_inputs(source)

        assert [(item.field, item.kind) for item in inputs] == [("Audio", "audio")]
        assert INPUT_KIND_EXTENSIONS[inputs[0].kind]  # …so the picker HAS a filter

    def test_a_declaration_the_prompt_forbids_costs_only_the_filter(self):
        """The stated price of reading the source instead of running it.

        A computed ``input_kinds`` — which rule 3b tells the model not to write — cannot be
        read without ``exec``, and ``exec`` before the risk banner is the one thing this flow
        must never do. So it falls back to a single text box, which is still a control the user
        can type into AND attach a file to; what is lost is the field's name and the filter.
        """
        source = _good_source("clip").replace(
            "    params_model: ClassVar[type] = ExtParams",
            "    params_model: ClassVar[type] = ExtParams\n"
            '    input_kinds = {n: "audio" for n in ("Audio",)}',
        )

        assert [(item.field, item.kind) for item in declared_inputs(source)] == [
            (SAMPLE_FIELD, "text")
        ]

    def test_a_refusal_is_surfaced_rather_than_treated_as_broken_code(self):
        """The model must be able to say no — and be believed.

        Rule 1 of the system prompt forbids prose, so a model asked for something the tool
        contract cannot express had no move except to invent a module. That is how a request to
        EXTRACT AUDIO from an mp4 came back as a tool that renames ".mp4" to ".mp3" in a
        string: plausible, clean, saves fine, and silently writes a reference to a file that
        was never created. Rule 0 gives it a way out, and this makes the way out land in front
        of the user instead of reading as a failed generation.
        """
        with pytest.raises(ProviderError, match="cannot be built as a tool") as excinfo:
            parse_user_tool_reply(
                "CANNOT: it needs to decode the video and re-encode its audio track.",
                "ext",
            )

        # The model's own reason reaches the user — a bare "cannot" teaches them nothing.
        assert "re-encode its audio track" in str(excinfo.value)
        # …and so does the boundary that caused it.
        assert "cannot read or write files" in str(excinfo.value)

    def test_a_refusal_with_no_reason_still_explains_the_boundary(self):
        with pytest.raises(ProviderError, match="cannot be built as a tool"):
            parse_user_tool_reply("CANNOT:", "ext")

    def test_code_that_merely_mentions_cannot_is_still_code(self):
        # The marker only counts at the very start of the reply; a tool whose comment or
        # message contains the word must not be mistaken for a refusal.
        code = parse_user_tool_reply(
            '@register_tool("user:ext")\n# CANNOT: happen mid-file\n', "ext"
        )

        assert code.startswith("@register_tool")

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
    ``tools_catalog``, which reads them outside the pipeline's per-attempt guard, taking the whole note
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


class TestTheDescriptionOutranksTheNoteType:
    """A description that names its fields must not be overridden by the note type's names.

    Reported from real use: a description asking for two sound fields "Audio 1" and "Audio 2"
    produced a tool whose params defaulted to the note type's OWN field names instead. Editing
    the description to name them explicitly changed nothing, because the user message was
    telling the model to prefer the note type — the instruction the user was fighting was the
    one we had written.
    """

    def test_the_message_says_the_description_wins(self):
        message = build_user_tool_message(
            "concat-audio",
            "two sound fields called Audio 1 and Audio 2, joined with a configurable gap",
            ["Audio", "Phát âm định nghĩa", "Front"],
        )

        # The regression guard: this exact instruction is what overrode the user, and it was in
        # the repo, so its absence is a real assertion rather than a vacuous one.
        assert "Use the most fitting of them as the DEFAULT" not in message
        assert "THE DESCRIPTION WINS" in message

    def test_the_note_type_fields_are_still_offered_as_a_fallback(self):
        # They are genuinely useful when the description names nothing — a param defaulting to
        # nothing is an input the test form cannot draw a control for.
        message = build_user_tool_message("x", "strip the html", ["Front", "Back"])

        assert "Front, Back" in message
        assert "ONLY when the description names no field" in message

    def test_no_note_type_fields_means_no_context_block(self):
        message = build_user_tool_message("x", "strip the html", [])

        assert "THE DESCRIPTION WINS" not in message
        assert "For context" not in message
