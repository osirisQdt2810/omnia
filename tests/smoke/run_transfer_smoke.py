r"""Drive every Export / Import control in the Smart Notes dialog against real Qt and a real
collection — the same way ``run_smoke.py`` does, and for the same reason.

``tests/conftest.py`` stubs ``aqt``/``anki``, so the pytest suite can prove the transfer LOGIC
(``tests/plugins/smart_notes/transfer/``) but can never prove that a button exists, that
clicking it reaches the right op, that the collision modal appears, or that a mapping select
the user changes ends up in what gets written. Those need the real page and the real bridge.

It does NOT go through ``aqt.run()``. Anki's single-instance key is
``anki{checksum(username)}`` — the USER, not the base folder — so a second Anki can never run
beside the developer's own. Building the pieces directly is what lets this run on a machine
with Anki already open, on either platform.

What it exercises, in order: the two footer buttons render → selecting a note type → Export
writes a file that parses as a bundle → Import opens the collision modal → clone mode creates
a second note type → overwrite mode shows the mapping table, refuses a duplicate target, and
applies → the remap rewrote all six places a field name is written down.

Run with Anki's bundled interpreter (see ``run_smoke.py`` for the per-platform invocation):
    QT_QPA_PLATFORM= "<AnkiProgramFiles>/.venv/bin/python" tests/smoke/run_transfer_smoke.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.append(str(_REPO / "vendor" / "universal"))
sys.path.insert(0, str(_REPO / "scripts"))

from common import enable_utf8_output  # noqa: E402

enable_utf8_output()

import aqt  # noqa: E402
from anki.collection import Collection  # noqa: E402
from aqt.qt import QApplication, QMainWindow, QMenu  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from omnia.core.config import ConfigLoader, ConfigRepository  # noqa: E402
from omnia.plugins.smart_notes.config import (  # noqa: E402
    FieldDep,
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine.tools.user_tools import (  # noqa: E402
    UserToolLoader,
    UserToolStore,
)
from omnia.plugins.smart_notes.transfer.bundle import parse_bundle  # noqa: E402
from omnia.plugins.smart_notes.transfer.collection import (  # noqa: E402
    SMART_NOTES_KEY,
    apply_bundle,
    plan_import,
)

SOURCE = "OmniaTransferSource"
TARGET = "OmniaTransferTarget"
SOURCE_FIELDS = ["Word", "Sentence", "Meaning"]
#: Deliberately different names, so the overwrite path has real mapping work to do.
TARGET_FIELDS = ["Term", "Example", "Gloss"]
CLONE_NAME = "Cloned Transfer"


class Runner:
    """Runs labelled checks in isolation and owns the exit code."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, label: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception:
            self.failures.append(label)
            print(f"FAIL {label}")
            traceback.print_exc()
            print("-" * 72)
            return None
        self.passed += 1
        detail = value if isinstance(value, str) else ""
        print(f"OK   {label}" + (f"\n     {detail}" if detail else ""))
        return value

    def report(self) -> int:
        print("=" * 72)
        if self.failures:
            print(f"{len(self.failures)} FAILED: {self.failures}")
            return 1
        print(f"ALL TRANSFER SMOKE CHECKS PASSED ({self.passed} checks)")
        return 0


def require(condition: bool, message: str) -> str:
    if not condition:
        raise AssertionError(message)
    return message


class AnkiStandIn(QMainWindow):
    """Anki's ``mw`` over a real throwaway collection — a QMainWindow so Qt accepts it as a
    parent (see ``run_smoke.py``, where a SimpleNamespace silently broke plugin enable).
    """

    def __init__(self, col: Any) -> None:
        super().__init__()
        self.col = col
        self.pm = SimpleNamespace(name="SmokeProfile", base=str(Path(col.path).parent))
        self.progress = SimpleNamespace(
            timer=lambda ms, cb, repeat: SimpleNamespace(stop=lambda: None),
            start=lambda **k: None,
            update=lambda **k: None,
            finish=lambda: None,
            want_cancel=lambda: False,
            # AnkiWebView.cleanup() defers its teardown through this on close; without it the
            # run ends in a traceback from the harness's own teardown, which reads exactly
            # like a product failure.
            single_shot=lambda ms, cb, *a, **k: cb(),
        )
        self.taskman = SimpleNamespace(
            run_in_background=lambda *a, **k: None,
            run_on_main=lambda cb: cb(),
        )
        # Deliberately NO ``mediaServer``. Anki 25.09's AnkiWebView routes setHtml through it
        # when it exists, and that path then wants a live ``mw.serverURL()`` — a real HTTP
        # server this stand-in has no business running. Absent, the webview falls back to
        # loading the HTML directly, which is what the page under test needs.
        self.app = app
        self.form = SimpleNamespace(menuTools=QMenu(self))

    def _increase_background_ops(self) -> None:
        return

    def _decrease_background_ops(self) -> None:
        return


def pump(seconds: float) -> None:
    """Let Qt and the page run for ``seconds`` without blocking the loop."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


class Page:
    """Evaluate JavaScript in the dialog's webview and get the answer back."""

    def __init__(self, dialog: Any) -> None:
        self._web = dialog._web

    def __call__(self, script: str, timeout: float = 8.0) -> Any:
        holder: dict[str, Any] = {}
        self._web.evalWithCallback(script, lambda value: holder.setdefault("v", value))
        deadline = time.time() + timeout
        while "v" not in holder and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        if "v" not in holder:
            raise AssertionError(f"the page did not answer: {script[:70]}")
        return holder["v"]

    def json(self, script: str) -> Any:
        return json.loads(self(script))

    def click(self, element_id: str, settle: float = 2.5) -> None:
        self(f"document.getElementById({json.dumps(element_id)}).click(); ''")
        pump(settle)


def seed_collection(col: Any) -> None:
    """Create both note types and a Smart Notes configuration on the source one."""
    for name, fields in ((SOURCE, SOURCE_FIELDS), (TARGET, TARGET_FIELDS)):
        model = col.models.new(name)
        for field in fields:
            col.models.add_field(model, col.models.new_field(field))
        template = col.models.new_template("Card 1")
        template["qfmt"] = "{{" + fields[0] + "}}"
        template["afmt"] = "{{FrontSide}}<hr id=answer>{{" + fields[1] + "}}"
        col.models.add_template(model, template)
        col.models.add(model)

    config = SmartNotesNoteTypeConfig(
        note_type=SOURCE,
        base_field="Word",
        node_positions={"Sentence": [10.0, 20.0], "Meaning": [30.0, 40.0]},
        fields=[
            SmartNotesFieldConfig(
                field="Sentence", enabled=True, prompt="Write a sentence for {{Word}}."
            ),
            SmartNotesFieldConfig(
                field="Meaning",
                enabled=True,
                prompt="Explain {{Word}} as used in {{Sentence}}.",
                depends_on=[FieldDep(field="Sentence", kind="hard", auto=True)],
                tools=[
                    FieldToolConfig(
                        tool="cloze",
                        params={
                            "sentence_field": "Sentence",
                            "word_field": "Word",
                            "mask": "none",
                        },
                    )
                ],
            ),
        ],
    )
    col.set_config(SMART_NOTES_KEY, {"note_types": [config.dict()]})


def config_entry(col: Any, note_type: str) -> Any:
    blob = col.get_config(SMART_NOTES_KEY, default={}) or {}
    for entry in blob.get("note_types", []):
        if entry.get("note_type") == note_type:
            return entry
    return None


def main() -> int:
    runner = Runner()
    workdir = Path(tempfile.mkdtemp(prefix="omnia-transfer-smoke-"))
    bundle_path = workdir / "exported.omnia-notetype.json"
    col = Collection(str(workdir / "collection.anki2"))
    aqt.mw = AnkiStandIn(col)
    seed_collection(col)

    from omnia.gui.smart_notes.dialogs.controllers import transfer as transfer_mod
    from omnia.gui.smart_notes.dialogs.studio import SmartNotesDialog

    # Substitute ONLY the OS file pickers: a native modal cannot be driven from inside the
    # event loop it blocks, and the dialog is chrome — the op, bundle and write are real.
    transfer_mod.TransferController._ask_save_path = lambda self, suggested: str(
        bundle_path
    )
    transfer_mod.TransferController._ask_open_path = lambda self: str(bundle_path)

    repo = ConfigRepository(ConfigLoader(workdir / "config"))
    dialog = SmartNotesDialog(repo, None)
    # The tools the bundle carries must land in the temp dir, never in the source tree.
    dialog._transfer._loader = UserToolLoader(UserToolStore(workdir / "tools"))
    dialog.show()
    pump(3.0)
    page = Page(dialog)

    try:
        runner.check("gui.both_buttons_render", lambda: _buttons_render(page))
        runner.check("gui.select_the_source_note_type", lambda: _select(page, SOURCE))
        runner.check("export.button_writes_a_file", lambda: _export(page, bundle_path))
        runner.check("export.file_is_a_valid_bundle", lambda: _bundle_ok(bundle_path))
        runner.check("export.footer_reports_success", lambda: _footer(page))
        runner.check(
            "import.button_opens_the_collision_modal", lambda: _open_import(page)
        )
        runner.check(
            "import.a_carried_tool_needs_explicit_approval",
            lambda: _tool_approval(page),
        )
        runner.check("import.clone_creates_a_second_note_type", lambda: _clone(page))
        runner.check(
            "import.clone_is_correct_in_the_collection", lambda: _clone_ok(col)
        )
        runner.check(
            "import.overwrite_shows_the_mapping_table", lambda: _overwrite_ui(page)
        )
        runner.check("import.duplicate_mapping_is_refused", lambda: _duplicate(page))
        runner.check("import.overwrite_applies", lambda: _apply_overwrite(page))
        runner.check(
            "remap.every_field_reference_was_rewritten",
            lambda: _remap(col, bundle_path, workdir),
        )
    finally:
        # The dialog is deliberately NOT closed. ``AnkiWebView.cleanup`` reaches for parts of
        # ``mw`` that only a real Anki has, and which parts differ by version (25.09 clears the
        # page through the media server, 26.8 does not) — and PyQt PRINTS an exception raised
        # inside ``closeEvent`` rather than propagating it, so it cannot even be suppressed.
        # Closing is not under test and every check has already run; the process exit frees the
        # window. Hiding it first keeps the run from ending with a window still on screen.
        dialog.hide()
        col.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return runner.report()


def _buttons_render(page: Page) -> str:
    data = page.json(
        "JSON.stringify({e: !!document.getElementById('sn-export'),"
        " i: !!document.getElementById('sn-import'),"
        " m: !!document.getElementById('sn-import-modal'),"
        " et: (document.getElementById('sn-export')||{}).textContent,"
        " it: (document.getElementById('sn-import')||{}).textContent})"
    )
    require(data["e"] and data["i"], "the footer is missing Export/Import")
    require(data["m"], "the page has no import modal")
    return f"Export={data['et']!r}  Import={data['it']!r}"


def _select(page: Page, note_type: str) -> str:
    page(
        "(function(){var s=document.getElementById('sn-note-type');"
        "for(var i=0;i<s.options.length;i++){if(s.options[i].text==="
        + json.dumps(note_type)
        + "){s.selectedIndex=i;"
        "s.dispatchEvent(new Event('change',{bubbles:true}));break;}}return '';})()"
    )
    pump(2.0)
    chosen = page("document.getElementById('sn-note-type').value")
    require(chosen == note_type, f"selected {chosen!r}")
    rows = page.json(
        "JSON.stringify(Array.prototype.map.call("
        "document.querySelectorAll('[data-field]'),"
        "function(r){return r.getAttribute('data-field');}))"
    )
    return f"{chosen}: rows={rows}"


def _export(page: Page, bundle_path: Path) -> str:
    if bundle_path.exists():
        bundle_path.unlink()
    page.click("sn-export", settle=3.0)
    require(bundle_path.is_file(), f"Export wrote nothing to {bundle_path}")
    return f"{bundle_path.name} ({bundle_path.stat().st_size} bytes)"


def _bundle_ok(bundle_path: Path) -> str:
    bundle = parse_bundle(bundle_path.read_text(encoding="utf-8"))
    require(bundle.note_type_name == SOURCE, bundle.note_type_name)
    require(bundle.field_names() == SOURCE_FIELDS, str(bundle.field_names()))
    require(bundle.smart_notes.base_field == "Word", bundle.smart_notes.base_field)
    require(len(bundle.smart_notes.fields) == 2, str(len(bundle.smart_notes.fields)))
    require(
        bundle.smart_notes.node_positions.get("Sentence") == [10.0, 20.0],
        str(bundle.smart_notes.node_positions),
    )
    return (
        f"{bundle.note_type_name}: fields={bundle.field_names()}, "
        f"rules={len(bundle.smart_notes.fields)}, graph={len(bundle.smart_notes.node_positions)}"
    )


def _footer(page: Page) -> str:
    text = page("(document.getElementById('sn-msg')||{}).textContent || ''")
    require("Exported" in text, f"the footer said {text!r}")
    return text


def _open_import(page: Page) -> str:
    page.click("sn-import", settle=3.0)
    state = page.json(
        "JSON.stringify({open: !document.getElementById('sn-import-modal').hidden,"
        " collision: !document.getElementById('sn-import-collision').hidden,"
        " rows: document.querySelectorAll('.sn-import-target').length,"
        " name: (document.getElementById('sn-import-newname')||{}).value})"
    )
    require(state["open"], "the import modal did not open")
    require(state["collision"], "a same-named note type exists — the modal must say so")
    require(state["rows"] == len(SOURCE_FIELDS), f"{state['rows']} mapping rows")
    return f"collision shown, {state['rows']} mapping rows, suggested name={state['name']!r}"


def _tool_approval(page: Page) -> str:
    """A bundle carrying tool code must not run it unasked.

    The seeded config uses no user tool, so the approval block must be ABSENT here — which is
    the assertion that the section is driven by what the file carries rather than always shown.
    A bundle that does carry one is covered headlessly in
    ``tests/plugins/smart_notes/transfer/test_imported_tools.py``.
    """
    state = page.json(
        "JSON.stringify({block: !!document.getElementById('sn-import-tools'),"
        " shown: !document.getElementById('sn-import-tools').hidden,"
        " boxes: document.querySelectorAll('.sn-import-tool-approve').length})"
    )
    require(state["block"], "the page has no tool-approval section at all")
    require(not state["shown"], "no tool is carried, so the section must stay hidden")
    require(state["boxes"] == 0, f"{state['boxes']} approval checkboxes for no tools")
    return (
        "approval section present and correctly hidden for a bundle carrying no tools"
    )


def _clone(page: Page) -> str:
    page(
        '(function(){var r=document.querySelector("input[name=sn-import-mode][value=clone]");'
        "r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));"
        "document.getElementById('sn-import-newname').value="
        + json.dumps(CLONE_NAME)
        + ";return '';})()"
    )
    pump(0.5)
    require(
        bool(page("document.getElementById('sn-import-mapping').hidden")),
        "clone mode must hide the field mapping",
    )
    page.click("sn-import-go", settle=3.5)
    result = page("document.getElementById('sn-import-result').textContent")
    require(CLONE_NAME in result, f"the modal said {result!r}")
    return result[:160]


def _clone_ok(col: Any) -> str:
    model = col.models.by_name(CLONE_NAME)
    require(model is not None, f"no {CLONE_NAME!r} note type was created")
    names = [f["name"] for f in model["flds"]]
    require(names == SOURCE_FIELDS, str(names))
    entry = config_entry(col, CLONE_NAME)
    require(entry is not None, "the clone has no Smart Notes configuration")
    require(entry["base_field"] == "Word", entry["base_field"])
    require(len(entry["fields"]) == 2, str(len(entry["fields"])))
    return f"fields={names}, base={entry['base_field']}, rules={len(entry['fields'])}"


def _overwrite_ui(page: Page) -> str:
    page.click("sn-import", settle=3.0)
    page(
        "(function(){var r=document.querySelector"
        '("input[name=sn-import-mode][value=overwrite]");'
        "r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));return '';})()"
    )
    pump(0.5)
    require(
        not page("document.getElementById('sn-import-mapping').hidden"),
        "overwrite mode must show the field mapping",
    )
    note = page("document.getElementById('sn-import-map-note').textContent")
    return f"mapping table shown; note={note!r}"


def _duplicate(page: Page) -> str:
    page(
        "(function(){var s=document.querySelectorAll('.sn-import-target');"
        "if(s.length>1){s[1].value=s[0].value;"
        "s[1].dispatchEvent(new Event('change',{bubbles:true}));}return '';})()"
    )
    pump(0.5)
    disabled = page("document.getElementById('sn-import-go').disabled")
    note = page("document.getElementById('sn-import-map-note').textContent")
    require(bool(disabled), "two fields onto one target must disable Import")
    require("only one" in note, f"the note said {note!r}")
    return f"Import disabled; note={note!r}"


def _apply_overwrite(page: Page) -> str:
    pairs = {name: name for name in SOURCE_FIELDS}
    page(
        "(function(){var m=" + json.dumps(pairs) + ";"
        "document.querySelectorAll('.sn-import-target').forEach(function(s){"
        "var want=m[s.getAttribute('data-source')];"
        "for(var i=0;i<s.options.length;i++){if(s.options[i].value===want){s.selectedIndex=i;"
        "s.dispatchEvent(new Event('change',{bubbles:true}));break;}}});return '';})()"
    )
    pump(0.5)
    require(
        not page("document.getElementById('sn-import-go').disabled"),
        "a valid 1:1 mapping must re-enable Import",
    )
    page.click("sn-import-go", settle=3.5)
    result = page("document.getElementById('sn-import-result').textContent")
    require("field rules" in result, f"the modal said {result!r}")
    return result[:160]


def _remap(col: Any, bundle_path: Path, workdir: Path) -> str:
    """Import onto the DIFFERENTLY-named note type and check all six rewrites."""
    bundle = parse_bundle(bundle_path.read_text(encoding="utf-8"))
    renames = dict(zip(SOURCE_FIELDS, TARGET_FIELDS, strict=True))
    plan = plan_import(
        col, bundle, mode="overwrite", target_name=TARGET, renames=renames
    )
    apply_bundle(
        col, bundle, plan, tool_loader=UserToolLoader(UserToolStore(workdir / "tools"))
    )

    entry = config_entry(col, TARGET)
    require(entry is not None, "no configuration was written for the target")
    require(entry["base_field"] == "Term", f"base_field={entry['base_field']}")
    fields = {f["field"]: f for f in entry["fields"]}
    require(set(fields) == {"Example", "Gloss"}, str(sorted(fields)))
    require(
        fields["Example"]["prompt"] == "Write a sentence for {{Term}}.",
        fields["Example"]["prompt"],
    )
    require(
        fields["Gloss"]["prompt"] == "Explain {{Term}} as used in {{Example}}.",
        fields["Gloss"]["prompt"],
    )
    require(
        [d["field"] for d in fields["Gloss"].get("depends_on", [])] == ["Example"],
        str(fields["Gloss"].get("depends_on")),
    )
    params = fields["Gloss"]["tools"][0]["params"]
    require(params["sentence_field"] == "Example", str(params))
    require(params["word_field"] == "Term", str(params))
    require(params["mask"] == "none", f"a non-field param was rewritten: {params}")
    positions = entry["node_positions"]
    require(positions.get("Example") == [10.0, 20.0], str(positions))
    require(positions.get("Gloss") == [30.0, 40.0], str(positions))
    return (
        "rule names, base_field, prompts, depends_on, node_positions and tool params "
        "all rewritten; the non-field param was left alone"
    )


if __name__ == "__main__":
    raise SystemExit(main())
