"""Headless UI smoke test for Omnia.

Drives the add-on the way clicking does — against a REAL ``anki.Collection`` + offscreen Qt +
real ``aqt.gui_hooks`` and a stand-in ``mw`` — firing every gui_hook Omnia subscribes and
constructing every dialog, with all plugins enabled. Each step is isolated; the first real
traceback per step is printed. Exit code is non-zero if any step fails.

Run with Anki's bundled interpreter:
    QT_QPA_PLATFORM=offscreen "<AnkiProgramFiles>/.venv/bin/python" scripts/ui_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import aqt  # noqa: E402
from aqt.qt import QApplication, QMenu  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
_failures: list[str] = []


def step(label, fn):
    try:
        fn()
        print(f"OK   {label}")
    except Exception:
        _failures.append(label)
        print(f"FAIL {label}")
        traceback.print_exc()
        print("-" * 72)


# --- real collection + a real card ----------------------------------------------------
from anki.collection import Collection  # noqa: E402

tmp = Path(tempfile.mkdtemp())
col = Collection(str(tmp / "collection.anki2"))
note = col.new_note(col.models.by_name("Basic"))
note["Front"], note["Back"] = "front", "back"
col.add_note(note, col.decks.id("Default"))
card = col.get_card(col.find_cards("")[0])
deck_id = col.decks.id("Default")

js_log: list[str] = []
web = SimpleNamespace(eval=lambda js: js_log.append(js))
mw = SimpleNamespace(
    col=col,
    reviewer=SimpleNamespace(
        card=card,
        state="answer",
        web=web,
        _showAnswer=lambda: None,
        _answerCard=lambda e: None,
    ),
    web=web,
    progress=SimpleNamespace(
        timer=lambda ms, cb, repeat: SimpleNamespace(stop=lambda: None),
        start=lambda **k: None,
        update=lambda **k: None,
        finish=lambda: None,
        want_cancel=lambda: False,
    ),
    taskman=SimpleNamespace(
        run_in_background=lambda *a, **k: None,
        run_on_main=lambda cb: cb(),
    ),
    form=SimpleNamespace(menuTools=QMenu()),
)
aqt.mw = mw

import shutil  # noqa: E402

# --- build the manager exactly like the entry point -----------------------------------
from omnia.core.config import ConfigLoader, ConfigRepository  # noqa: E402
from omnia.core.manager import PluginManager  # noqa: E402
from omnia.core.plugin import AddonPaths  # noqa: E402

src = REPO / "src" / "omnia"
# Toggling plugins persists overrides — copy the real config into the temp dir so the smoke
# test reads the real creds but NEVER mutates the user's actual user_files/omnia.toml.
tmp_override = tmp / "omnia.toml"
real_override = src / "user_files" / "omnia.toml"
if real_override.exists():
    shutil.copy(real_override, tmp_override)
paths = AddonPaths(src, src / "web", tmp)
repo = ConfigRepository(ConfigLoader(src / "config", tmp_override))
mgr = PluginManager(repo, paths)
step("manager.setup()", mgr.setup)
step(
    "enable EVERY plugin (toggles)",
    lambda: [mgr.set_enabled(p.id, True) for p in mgr.plugins()],
)
print("    active plugins:", sorted(mgr._active))

from aqt import gui_hooks  # noqa: E402

# --- fire every gui_hook Omnia subscribes (with all plugins on) -----------------------
step("reviewer_did_show_question", lambda: gui_hooks.reviewer_did_show_question(card))
step("reviewer_did_show_answer", lambda: gui_hooks.reviewer_did_show_answer(card))
step(
    "reviewer_will_answer_card (filter)",
    lambda: gui_hooks.reviewer_will_answer_card((3, False), mw.reviewer, card),
)
step(
    "av_player_did_end_playing",
    lambda: gui_hooks.av_player_did_end_playing(SimpleNamespace()),
)
step(
    "webview_did_receive_js_message (non-omnia)",
    lambda: gui_hooks.webview_did_receive_js_message(
        (False, None), "deckbrowser:open", None
    ),
)
step(
    "webview_did_receive_js_message (typed_accuracy 'rated')",
    lambda: gui_hooks.webview_did_receive_js_message(
        (False, None),
        'omnia:{"plugin":"typed_accuracy","op":"rated","data":{"ratio":0.9}}',
        None,
    ),
)


def _editor_stub():
    return SimpleNamespace(
        note=note,
        web=web,
        currentField=0,
        parentWindow=None,
        addButton=lambda **kw: "<button>",
        loadNote=lambda: None,
        loadNoteKeepingFocus=lambda: None,
    )


def fire_editor_buttons():
    buttons: list = []
    gui_hooks.editor_did_init_buttons(buttons, _editor_stub())
    assert buttons, "smart_notes did not add an editor button"


step("editor_did_init_buttons (✨ button)", fire_editor_buttons)
step(
    "editor_will_show_context_menu (field menu)",
    lambda: gui_hooks.editor_will_show_context_menu(
        SimpleNamespace(editor=_editor_stub()), QMenu()
    ),
)
step(
    "browser_will_show_context_menu",
    lambda: gui_hooks.browser_will_show_context_menu(
        SimpleNamespace(selectedNotes=lambda: [note.id]), QMenu()
    ),
)
step(
    "browser_sidebar_will_show_context_menu (deck batch)",
    lambda: gui_hooks.browser_sidebar_will_show_context_menu(
        SimpleNamespace(),
        QMenu(),
        SimpleNamespace(
            item_type=SimpleNamespace(name="DECK"),
            full_name="Default",
            name="Default",
        ),
        0,
    ),
)
step(
    "deck_browser_will_show_options_menu (gear menu)",
    lambda: gui_hooks.deck_browser_will_show_options_menu(QMenu(), deck_id),
)


def fire_overview():
    content = SimpleNamespace(table="<table></table>")
    gui_hooks.overview_will_render_content(SimpleNamespace(mw=mw), content)


step("overview_will_render_content (stats card)", fire_overview)

# --- construct every Configure dialog (clicking "Configure…") -------------------------
from omnia.gui.config_form import PluginConfigDialog  # noqa: E402


def open_generic_config(pid):
    plugin = next(p for p in mgr.plugins() if p.id == pid)
    s = repo.feature_settings(pid)
    PluginConfigDialog(plugin.name, plugin.config_schema(), s.dict() if s else {}, None)


for _pid in ("auto_flip", "typed_accuracy", "overdue_guard"):
    step(
        f"PluginConfigDialog[{_pid}] (generic Configure form)",
        lambda pid=_pid: open_generic_config(pid),
    )


def build_smart_notes_dialog():
    """Construct the SmartNotes WebDialog and drive its synchronous pycmd ops via the bridge.

    Auto-smart is deliberately NOT fired here: it runs a real LLM off-thread and needs creds +
    network, so the smoke only exercises the offline ops (list/load/set_base_field/create/save)
    through the same ``_on_cmd`` envelope the webview uses.
    """
    from omnia.core.reviewer.web_injector import build_message
    from omnia.gui.smart_notes.dialog import SmartNotesDialog

    dialog = SmartNotesDialog(repo, None)

    def op(name, data):
        return dialog._on_cmd(build_message("smart_notes", name, data))

    names = op("list_note_types", {})
    assert names and "Basic" in names, f"list_note_types returned {names!r}"
    loaded = op("load", {"note_type": "Basic"})
    assert loaded["base_field"] == "Front", loaded
    assert [r["field"] for r in loaded["rows"]] == ["Back"], loaded
    assert loaded["providers"], "no LLM providers listed"
    rebased = op("set_base_field", {"note_type": "Basic", "base_field": "Back"})
    assert [r["field"] for r in rebased["rows"]] == ["Front"], rebased
    created = op("create_field", {"note_type": "Basic", "field_name": "Example"})
    assert "Example" in created.get("all_fields", []), created
    saved = op(
        "save",
        {
            "note_type": "Basic",
            "base_field": "Front",
            "rows": [
                {
                    "field": "Back",
                    "enabled": True,
                    "type": "text",
                    "prompt": "Define {{Front}}",
                },
                {
                    "field": "Example",
                    "enabled": True,
                    "type": "text",
                    "prompt": "Use {{Front}}",
                },
            ],
        },
    )
    assert saved == {"ok": True}, saved
    # The save must round-trip back through the repo into the typed config.
    reloaded = ConfigRepository(ConfigLoader(src / "config", tmp_override))
    nt = reloaded.feature_settings("smart_notes").note_type_config("Basic")
    assert nt is not None and nt.base_field == "Front", nt
    assert {f.field for f in nt.generatable_fields()} == {"Back", "Example"}, nt


step("SmartNotesDialog (custom Configure + pycmd ops)", build_smart_notes_dialog)


def build_prompt_dialog():
    from omnia.gui.smart_notes.prompt_dialog import PromptDialog
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule

    rule = SmartNotesFieldRule(note_type="Basic", target_field="Back", kind="text")
    PromptDialog(repo, rule, lambda _saved: None, None)


step("PromptDialog (add/edit one rule)", build_prompt_dialog)
step(
    "CustomPromptDialog (one-off custom text)",
    lambda: __import__(
        "omnia.gui.smart_notes.custom_prompt", fromlist=["CustomPromptDialog"]
    ).CustomPromptDialog(
        repo,
        kind="text",
        note_type="Basic",
        field_names=["Front", "Back"],
        target_field="Back",
        on_save=lambda _v: None,
        parent=None,
    ),
)
step(
    "AutoFlipDeckDialog (per-deck options)",
    lambda: __import__(
        "omnia.gui.auto_flip.deck_options", fromlist=["AutoFlipDeckDialog"]
    ).AutoFlipDeckDialog(deck_id, repo.feature_settings("auto_flip"), None),
)
step(
    "SettingsDialog (Tools -> Omnia)",
    lambda: __import__(
        "omnia.gui.settings_dialog", fromlist=["SettingsDialog"]
    ).SettingsDialog(mgr, None),
)

# --- disable everything (untick) ------------------------------------------------------
step(
    "disable EVERY plugin (untick)",
    lambda: [mgr.set_enabled(p.id, False) for p in mgr.plugins()],
)

col.close()
print(
    f"\n{'='*72}\n{len(_failures)} FAILED step(s): {_failures}"
    if _failures
    else f"\n{'='*72}\nALL UI SMOKE STEPS PASSED ({len(js_log)} JS evals captured)"
)
sys.exit(1 if _failures else 0)
