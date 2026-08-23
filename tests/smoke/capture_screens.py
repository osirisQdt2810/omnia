"""Capture the real Omnia dialogs to PNG, for the user guide.

Screenshots for a public page have to come from somewhere, and the two obvious sources are both
wrong. Mock-ups drift from the product the moment either changes. The author's own Anki shows
the author's own decks, which is personal data on a page anyone can read.

So this reuses the smoke harness: it already stands up real ``aqt``/``anki``/Qt against a
THROWAWAY collection and constructs every plugin's Configure dialog. The widgets are the real
ones, and the only content in them is what a fresh install would show.

Run with Anki's bundled interpreter, same as the smoke:
    "<AnkiProgramFiles>/.venv/bin/python" tests/smoke/capture_screens.py <out-dir>
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent.parent / "scripts"))

from common import enable_utf8_output  # noqa: E402  (path set above)

# This script prints paths and sizes, and Windows consoles default to cp1252 -- one em dash in
# a filename is enough to kill the run. Same guard every other printing entry point arms.
enable_utf8_output()

import run_smoke as smoke_mod  # noqa: E402  (path set above)


def _save(widget, path: Path, *, settle_ms: int = 400) -> tuple[int, int]:
    """Render ``widget`` to ``path``; return the pixmap size actually written.

    A webview needs its own event loop turns before it has painted anything, which is why this
    pumps rather than grabbing immediately — an un-pumped QWebEngineView grabs as a blank
    rectangle and the failure looks like a styling bug rather than a timing one.
    """
    from aqt.qt import QApplication, QEventLoop, QTimer

    widget.resize(widget.sizeHint().expandedTo(widget.minimumSizeHint()))
    widget.show()
    loop = QEventLoop()
    QTimer.singleShot(settle_ms, loop.quit)
    loop.exec()
    QApplication.processEvents()
    pix = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(path), "PNG")
    return pix.width(), pix.height()



_seeded_name = [""]


def _seed_demo_content(shot) -> None:
    """Seed the throwaway collection from a REAL note-type config supplied on the command line.

    Screenshots of an unconfigured screen sell nothing, and a synthetic config never quite looks
    like a real one. So the capture takes a config blob (``--config <file>``) holding one
    note type's Smart Notes setup, recreates that note type's FIELDS in the throwaway
    collection, and writes the config verbatim.

    What that publishes is the CONFIGURATION -- field names, tool chains, prompts -- and never a
    note. The collection has no notes in it at all, so no deck content can leak into an image.
    """
    import json
    import os

    blob_path = os.environ.get("OMNIA_SHOT_CONFIG")
    if not blob_path:
        print("no OMNIA_SHOT_CONFIG set; leaving the collection empty")
        return
    nt_cfg = json.loads(pathlib.Path(blob_path).read_text(encoding="utf-8"))

    col = shot.anki.col if hasattr(shot, "anki") else shot.col
    name = nt_cfg["note_type"]
    model = col.models.new(name)
    seen = set()
    for f in [{"field": nt_cfg["base_field"]}] + nt_cfg["fields"]:
        fname = f["field"]
        if fname in seen:
            continue
        seen.add(fname)
        col.models.add_field(model, col.models.new_field(fname))
    tmpl = col.models.new_template("Card 1")
    tmpl["qfmt"] = "{{" + nt_cfg["base_field"] + "}}"
    tmpl["afmt"] = "{{FrontSide}}"
    col.models.add_template(model, tmpl)
    col.models.add(model)

    payload = {"note_types": [nt_cfg]}
    shot.repo.update_section("smart_notes", payload)
    # ADR-006: at runtime the dialog reads smart-notes settings out of the COLLECTION config,
    # not the file-backed repository the smoke harness builds. Writing only the repo leaves the
    # screen rendering defaults -- every toggle off -- while a read-back of the repo looks fine,
    # which is exactly how this cost an hour.
    try:
        col.set_config("omnia:smart_notes", payload)
    except Exception as exc:
        print("could not write collection config:", exc)
    _seeded_name[0] = name
    print(f"seeded {name}: {len(seen)} fields, "
          f"{sum(1 for f in nt_cfg['fields'] if f.get('enabled'))} enabled")


def _run_js(dialog, script: str, *, settle_ms: int = 1200) -> None:
    """Run ``script`` inside the dialog's webview and let it settle.

    The Smart Notes screen is a webview, so its note-type selection and its Fields/Dependencies
    tab live in the DOM, not in Qt. Selecting them from Python is the only way to photograph the
    screen in a state a real user would recognise -- the dialog opens on whichever note type
    sorts first, which in a throwaway collection is the stock "Basic".
    """
    from aqt.qt import QEventLoop, QTimer

    view = None
    for attr in ("web", "_web", "webview", "_webview", "view"):
        view = getattr(dialog, attr, None)
        if view is not None and hasattr(view, "page"):
            break
    if view is None:
        for child in dialog.findChildren(object):
            if type(child).__name__ in ("AnkiWebView", "QWebEngineView"):
                view = child
                break
    if view is None:
        raise RuntimeError("no webview on " + type(dialog).__name__)
    view.page().runJavaScript(script)
    loop = QEventLoop()
    QTimer.singleShot(settle_ms, loop.quit)
    loop.exec()


_SELECT_DEMO = """
(() => {
  const sel = document.getElementById('sn-note-type');
  if (!sel) return 'no select';
  // Pick the seeded note type by name; "Basic" is the stock one the throwaway ships with.
  const want = window.__shotNoteType;
  const opt = [...sel.options].find(o => o.textContent.trim() === want);
  if (!opt) return 'demo not listed';
  sel.value = opt.value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return 'ok';
})()
"""

_SHOW_GRAPH = "document.getElementById('sn-view-graph')?.click()"

def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "docs/images").resolve()
    workdir = Path(tempfile.mkdtemp(prefix="omnia-shots-"))
    anki = smoke_mod.AnkiStandIn.build(workdir)
    shot = smoke_mod.OmniaSmoke(anki, workdir)
    shot.manager.setup()
    for pid in shot.plugin_ids():
        shot.manager.set_enabled(pid, True)

    _seed_demo_content(shot)

    # The real screen is dark: page.css swaps to #1b1e27/#14161d under night mode, and that is
    # what a user actually sees. Capturing the light variant photographs a theme most people
    # never look at.
    try:
        from aqt import theme as _theme
        _theme.theme_manager.night_mode = True
        _theme.theme_manager._night_mode_preference = True
    except Exception as exc:
        print("could not force night mode:", exc)

    # Read the seeded config back: if the toggles render off, the write did not land.
    _seeded = shot.repo.feature_settings("smart_notes")
    _nt = getattr(_seeded, "note_types", None) if _seeded else None
    print("seeded note_types:", [(n.note_type, n.base_field,
          [(f.field, f.enabled) for f in n.fields]) for n in (_nt or [])][:1])

    from omnia.gui.config_form import PluginConfigDialog

    written: list[str] = []
    for plugin in shot.manager.plugins():
        if plugin.has_custom_config_dialog():
            dialog = plugin.custom_config_dialog(shot.repo, None)
        else:
            settings = shot.repo.feature_settings(plugin.id)
            dialog = PluginConfigDialog(
                plugin.name or plugin.id,
                plugin.config_schema(),
                settings.dict() if settings is not None else {},
                None,
            )
        if dialog is None:
            print(f"SKIP {plugin.id}: no dialog")
            continue
        # A webview dialog needs longer than a plain Qt form before it has painted.
        settle = 2500 if type(dialog).__name__ != "PluginConfigDialog" else 400
        try:
            if plugin.id == "smart_notes":
                # Two shots of the flagship: the field table on a configured note type, and the
                # dependency graph. Both need the demo note type selected inside the webview.
                _save(dialog, out / "_warm.png", settle_ms=settle)
                _run_js(dialog, f"window.__shotNoteType = {json.dumps(_seeded_name[0])};",
                        settle_ms=200)
                _run_js(dialog, _SELECT_DEMO, settle_ms=2500)
                w, h = _save(dialog, out / "smart_notes-fields.png", settle_ms=600)
                written.append(f"OK   smart_notes-fields.png  {w}x{h}")
                _run_js(dialog, _SHOW_GRAPH, settle_ms=2000)
                w, h = _save(dialog, out / "smart_notes-graph.png", settle_ms=600)
                written.append(f"OK   smart_notes-graph.png  {w}x{h}")
                (out / "_warm.png").unlink(missing_ok=True)
            else:
                w, h = _save(dialog, out / f"{plugin.id}.png", settle_ms=settle)
                written.append(f"OK   {plugin.id}.png  {w}x{h}  ({type(dialog).__name__})")
        except Exception as exc:
            written.append(f"FAIL {plugin.id}: {type(exc).__name__}: {exc}")
        try:
            dialog.close()
        except Exception:
            pass

    anki.col.close()
    print("\n".join(written) or "(nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
