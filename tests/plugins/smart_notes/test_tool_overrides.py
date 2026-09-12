"""Tests for editing a builtin: the user's own copy of a shipped tool, loaded over it.

The properties that make this safe to run at plugin start, each pinned below:

* **The edit reaches the fields already using the tool.** An override registers the BUILTIN's
  name, so a chain that says ``cloze`` runs the user's version with nothing to re-pick — which
  is the only thing "edit the builtin" can honestly mean.
* **A broken edit never costs the builtin.** The file is compiled before it is written and again
  on every load; either failure leaves the shipped class registered.
* **The shipped class comes back exactly.** Restoring is putting the remembered class back, not
  re-importing a module and hoping.
* **The user-tool rule is untouched.** A file in ``user_files/tools/`` still may not claim a
  builtin's name; only the ``builtin/`` subdirectory can, which is what the user's Edit writes.

Every file is written into ``tmp_path``, and the registry is restored after each test.
"""

from __future__ import annotations

import logging

import pytest

from omnia.plugins.smart_notes.engine.tools import (
    TOOL_REGISTRY,
    BuiltinOverrideLoader,
    BuiltinOverrideStore,
    ClozeTool,
    UserToolError,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    builtin_tool_source,
    get_tool,
    overridable_tools,
)
from omnia.plugins.smart_notes.engine.tools import overrides as overrides_module
from omnia.plugins.smart_notes.engine.tools import (
    validate_builtin_name,
)

# A complete, tiny replacement for the `cloze` tool: enough to be a real Tool subclass, and
# recognisable in an assertion.
OVERRIDE_CODE = """
from typing import ClassVar

from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools.base import Produced, Tool, ToolOutcome
from omnia.plugins.smart_notes.engine.tools.registry import register_tool


@register_tool("cloze")
class MyCloze(Tool):
    label: ClassVar[str] = "My cloze"
    description: ClassVar[str] = "The user's own version"
    kinds: ClassVar[frozenset] = frozenset({"text"})
    deterministic: ClassVar[bool] = True

    def run(self, request, ctx) -> ToolOutcome:
        return Produced(GenerationResult("text", text="mine"))
"""


@pytest.fixture
def registry_guard():
    """Restore the tool registry — and the displaced-builtin memory — after a test.

    ``_SHIPPED`` is module state for the same reason the registry is, so a test that overrides
    a builtin has to put both back or the next one starts with a lie about what shipped.
    """
    before = dict(TOOL_REGISTRY)
    shipped = dict(overrides_module._SHIPPED)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(before)
    overrides_module._SHIPPED.clear()
    overrides_module._SHIPPED.update(shipped)


@pytest.fixture
def store(tmp_path):
    """An override store over a throwaway directory (never the repo's own tree)."""
    return BuiltinOverrideStore(tmp_path / "tools" / "builtin")


@pytest.fixture
def loader(store, registry_guard):
    """A loader over the throwaway store, with the registry restored afterwards."""
    return BuiltinOverrideLoader(store, log=logging.getLogger("omnia.test"))


class TestWhatCanBeEdited:
    def test_the_builtins_are_offered_and_user_tools_are_not(self):
        names = overridable_tools()

        assert {"ai", "cloze", "cloze_audio"} <= set(names)
        assert not [name for name in names if name.startswith("user:")]

    def test_a_builtin_name_may_hold_an_underscore(self):
        # `cloze_audio` is a real builtin and would fail the user-tool slug rule, which is why
        # this validator exists at all.
        assert validate_builtin_name("cloze_audio") == "cloze_audio"

    @pytest.mark.parametrize(
        "name", ["", "Cloze", "../etc/passwd", "cloze audio", "9lives"]
    )
    def test_anything_that_is_not_a_name_is_refused(self, name):
        # The name is also the file name, so this is what keeps a save inside the folder.
        with pytest.raises(UserToolError):
            validate_builtin_name(name)


class TestReadingTheShippedSource:
    def test_it_returns_the_real_module(self):
        code = builtin_tool_source("cloze")

        assert '@register_tool("cloze")' in code
        assert "class ClozeTool" in code

    def test_every_builtin_can_be_opened(self):
        # If one could not be read, its Edit button would be a dead end — worth knowing here
        # rather than from a user whose click did nothing.
        for name in overridable_tools():
            assert builtin_tool_source(name).strip()

    def test_an_unknown_tool_says_so(self):
        with pytest.raises(UserToolError, match="no tool called"):
            builtin_tool_source("nosuchtool")


class TestSavingAnOverride:
    def test_the_builtin_name_now_resolves_to_the_users_class(self, loader):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        assert get_tool("cloze").__name__ == "MyCloze"
        assert loader.overridden() == ("cloze",)

    def test_the_file_lands_under_the_builtin_directory(self, loader, store):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        assert (store.directory / "cloze.py").is_file()
        assert store.slugs() == ["cloze"]

    def test_code_that_cannot_load_is_never_written(self, loader, store):
        with pytest.raises(UserToolError):
            loader.save(UserToolSource(slug="cloze", code="this is not python"))

        # Both halves matter: no file to load on the next start, and the builtin still running.
        assert store.slugs() == []
        assert get_tool("cloze") is ClozeTool

    def test_code_that_registers_the_wrong_name_is_refused(self, loader):
        # An override may claim the name of the file it is in, and nothing else. (The refusal
        # comes from the registry itself here — the name it reached for belongs to another
        # builtin — which is why the message names that collision rather than the file's own.)
        wrong = OVERRIDE_CODE.replace('register_tool("cloze")', 'register_tool("ai")')

        with pytest.raises(UserToolError):
            loader.save(UserToolSource(slug="cloze", code=wrong))

        assert get_tool("cloze") is ClozeTool
        assert get_tool("ai").__name__ != "MyCloze"

    def test_saving_twice_still_remembers_the_shipped_class(self, loader):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))
        loader.save(
            UserToolSource(slug="cloze", code=OVERRIDE_CODE.replace("mine", "mine2"))
        )

        loader.restore("cloze")

        # Not the first override — the class that shipped. Remembering the displaced class on
        # every save would have restored the user's own previous version instead.
        assert get_tool("cloze") is ClozeTool


class TestRestoring:
    def test_it_deletes_the_file_and_puts_the_builtin_back(self, loader, store):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        assert loader.restore("cloze") is True
        assert store.slugs() == []
        assert get_tool("cloze") is ClozeTool

    def test_restoring_what_was_never_overridden_is_harmless(self, loader):
        assert loader.restore("cloze") is False
        assert get_tool("cloze") is ClozeTool

    def test_disabling_the_feature_restores_every_builtin(self, loader):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        loader.unload_all()

        # The user tools' teardown sweeps a NAMESPACE; doing that here would unregister the
        # builtins themselves, so this one walks its own bookkeeping.
        assert get_tool("cloze") is ClozeTool
        assert get_tool("ai") is not None
        assert loader.overridden() == ()


class TestTwoLoadersOneRegistry:
    """A running Anki has TWO: the plugin builds one at enable, the dialog one when it opens.

    They bind the same registry, so the memory of what an override displaced cannot live on
    either instance. Held per loader, the one that restores is not the one that displaced —
    and every assertion below passed while that was the case, because a single-loader fixture
    cannot see it.
    """

    def _second(self, store):
        """A second loader over the same folder — the dialog's, to the plugin's."""
        return BuiltinOverrideLoader(store, log=logging.getLogger("omnia.test"))

    def test_restore_from_the_dialog_undoes_the_plugins_load(self, loader, store):
        # 1. Anki starts with an override on disk, 2. the dialog opens and reloads the same
        # file, 3. the user presses Restore built-in.
        store.write(UserToolSource(slug="cloze", code=OVERRIDE_CODE))
        loader.load_all()
        dialog = self._second(store)
        dialog.load_all()

        dialog.restore("cloze")

        # It used to re-register the DIALOG's own copy of the user's class: the card said the
        # builtin was back and every generation for the rest of the session ran deleted code.
        assert get_tool("cloze") is ClozeTool
        assert store.slugs() == []

    def test_disabling_restores_an_override_saved_in_the_dialog(self, loader, store):
        # The plugin's loader never hears about a save made through the dialog's, so a teardown
        # keyed on its own bookkeeping walked an empty set and left the user's class running
        # with the feature switched off.
        dialog = self._second(store)
        dialog.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        loader.unload_all()

        assert get_tool("cloze") is ClozeTool

    def test_both_loaders_report_the_same_overridden_set(self, loader, store):
        dialog = self._second(store)
        dialog.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        assert loader.overridden() == ("cloze",)
        assert dialog.overridden() == ("cloze",)

    def test_a_user_tool_and_an_override_of_the_same_name_keep_their_modules(
        self, tmp_path, store, registry_guard
    ):
        # Both files are called "cloze.py", in different folders. One sys.modules key for both
        # would make a class's __module__ — and so the source the editor shows — point at
        # whichever loaded last.
        user_loader = UserToolLoader(
            UserToolStore(tmp_path / "tools"), log=logging.getLogger("omnia.test")
        )
        user_code = OVERRIDE_CODE.replace(
            'register_tool("cloze")', 'register_tool("user:cloze")'
        )
        user_loader.store.write(UserToolSource(slug="cloze", code=user_code))
        override_loader = BuiltinOverrideLoader(store)
        override_loader.store.write(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        user_loader.load_all()
        override_loader.load_all()

        assert get_tool("user:cloze").__module__ != get_tool("cloze").__module__
        assert "class MyCloze" in builtin_tool_source("cloze")


class TestLoadingAtStartup:
    def test_a_file_in_the_folder_is_loaded(self, loader, store):
        store.write(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        loads = loader.load_all()

        assert [load.slug for load in loads] == ["cloze"]
        assert get_tool("cloze").__name__ == "MyCloze"

    def test_a_broken_file_is_reported_and_the_builtin_keeps_running(
        self, loader, store
    ):
        store.write(UserToolSource(slug="cloze", code="raise RuntimeError('boom')"))

        (load,) = loader.load_all()

        assert not load.ok
        assert "boom" in load.error
        assert get_tool("cloze") is ClozeTool

    def test_a_file_that_disappeared_stops_being_registered(self, loader, store):
        loader.save(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        store.delete("cloze")
        loader.load_all()

        assert get_tool("cloze") is ClozeTool


class TestTheUserToolRuleIsUntouched:
    def test_a_user_tool_still_cannot_claim_a_builtin(self, tmp_path, registry_guard):
        # The override path is the ONLY way to a builtin's name. A file in the tools folder
        # proper is still refused whatever its decorator says.
        user_loader = UserToolLoader(
            UserToolStore(tmp_path / "tools"), log=logging.getLogger("omnia.test")
        )
        user_loader.store.write(UserToolSource(slug="mine", code=OVERRIDE_CODE))

        (load,) = user_loader.load_all()

        assert not load.ok
        assert "'cloze' already registered" in load.error
        assert get_tool("cloze") is ClozeTool

    def test_the_two_folders_do_not_see_each_others_files(
        self, tmp_path, registry_guard
    ):
        # The overrides live in a SUBdirectory, so the user-tool store's `*.py` glob must not
        # pick them up and report a tool the user never wrote.
        user_store = UserToolStore(tmp_path / "tools")
        override_store = BuiltinOverrideStore(tmp_path / "tools" / "builtin")
        override_store.write(UserToolSource(slug="cloze", code=OVERRIDE_CODE))

        assert user_store.slugs() == []
        assert override_store.slugs() == ["cloze"]
